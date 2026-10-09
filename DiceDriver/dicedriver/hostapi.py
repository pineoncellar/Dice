"""The 39 host APIs Dice calls (docs/DiceDriver-interface-duties.md sections 3 and 5).

`HostApi.dispatch(name, ints, text)` is pure Python (no ctypes) so it can be unit tested. Handlers
return a python value and the dispatcher maps it to the shim's wire format:
  bool/int -> return value;  str -> UTF-8 out buffer;  bytes -> raw out buffer (ANSI paths);
  set[int] -> "1,2,3" out buffer;  (bool, str) -> return value + out buffer (DiceUpdate).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import logs
from .cq import cq_to_segments
from .state import UNKNOWN, Member, State

log = logging.getLogger("dd.api")
dice_log = logging.getLogger("dice")

DRIVER_VERSION = "DiceDriver-py v0.1 (OneBot11)"

# Interfaces the project decided NOT to implement; they must stay absent from the table.
DROPPED = frozenset({
    "DiceUploadBlack", "GetExtra", "SendDiscussMsg", "SendChannelMsg", "IsDiceMaid", "GetDiceSisters",
})

# APIs whose last integer argument is Dice's own default for "could not find out".
_HAS_DEFAULT = frozenset({"IsFriend", "IsGroupAdmin", "IsGroupOwner", "IsGroupMember"})
_DEBUG_QUIET = frozenset({"DebugLog", "PrintGroupInfo", "_DriverVer"})  # chatty; trace only


@dataclass
class Result:
    ret: int = 0
    out: bytes = b""


class HostApi:
    def __init__(self, cfg, bot, state: State, notifier, heartbeat, lifecycle):
        """lifecycle: object with restart(reason: str), exit(), root_dir: Path, self_id: int."""
        self.cfg = cfg
        self.bot = bot
        self.state = state
        self.notifier = notifier
        self.heartbeat = heartbeat
        self.lifecycle = lifecycle

    # ------------------------------------------------------------------ dispatch

    def names(self) -> list[str]:
        return [n for n in dir(self) if n[:1].isupper() or n == "_DriverVer"]

    def dispatch(self, name: str, ints: tuple[int, ...], text_bytes: bytes) -> Result:
        handler = getattr(self, name, None)
        text = text_bytes.decode("utf-8", errors="surrogateescape")
        t0 = time.monotonic()
        try:
            if handler is None:
                raise LookupError(f"no handler for {name}")
            value = handler(ints, text)
        except Exception:
            log.exception("API %s%s raised; returning the fallback", name, ints)
            return self._fallback(name, ints)
        result = self._wire(value)
        ms = (time.monotonic() - t0) * 1000
        # queries are trace-only; anything that acts (send/kick/answer/...) is visible at debug
        quiet = name.startswith(("Get", "Is")) or name in _DEBUG_QUIET
        level = logging.WARNING if ms >= 500 else (logs.TRACE if quiet else logging.DEBUG)
        log.log(level, "%s(%s) -> %s %.1fms", name, self._brief(ints, text), self._brief_ret(result), ms)
        return result

    @staticmethod
    def _brief(ints: tuple[int, ...], text: str) -> str:
        parts = [str(i) for i in ints]
        if text:
            parts.append(repr(text if len(text) <= 200 else text[:200] + f"...({len(text)} chars)"))
        return ", ".join(parts)

    @staticmethod
    def _brief_ret(r: Result) -> str:
        out = r.out.decode("utf-8", errors="replace")
        return f"ret={r.ret}" + (f" out={out[:120]!r}" if out else "")

    @staticmethod
    def _wire(value: Any) -> Result:
        if value is None:
            return Result()
        if isinstance(value, bool):
            return Result(1 if value else 0)
        if isinstance(value, int):
            return Result(value)
        if isinstance(value, str):
            return Result(0, value.encode("utf-8", errors="surrogateescape"))
        if isinstance(value, bytes):
            return Result(0, value)
        if isinstance(value, (set, frozenset)):
            return Result(0, ",".join(str(i) for i in sorted(value)).encode())
        if isinstance(value, tuple) and len(value) == 2:
            return Result(1 if value[0] else 0, str(value[1]).encode("utf-8"))
        raise TypeError(f"unsupported handler result: {type(value)}")

    @staticmethod
    def _fallback(name: str, ints: tuple[int, ...]) -> Result:
        if name in _HAS_DEFAULT and ints:
            return Result(1 if ints[-1] else 0)
        if name == "GetGroupLastMsg":
            return Result(-1)
        return Result()

    # ------------------------------------------------------------------ helpers

    @property
    def _self_id(self) -> int:
        return self.lifecycle.self_id

    def _pack_message(self, text: str):
        if self.cfg.onebot.send_format == "array":
            return cq_to_segments(text)
        return text

    @staticmethod
    def _resolve_path(text: str) -> Path:
        """Dice may hand over UTF-8 or the ANSI code page; pick whichever exists."""
        raw = text.encode("utf-8", errors="surrogateescape")
        candidates = []
        for enc in ("utf-8", "mbcs"):
            try:
                candidates.append(Path(raw.decode(enc)))
            except (UnicodeDecodeError, LookupError):
                pass
        for p in candidates:
            if p.exists():
                return p.resolve()
        return (candidates[0] if candidates else Path(text)).resolve()

    def _warn_unsupported(self, what: str) -> None:
        log.warning("%s has no OneBot 11 equivalent; ignored", what)

    # ------------------------------------------------------------------ A. host info

    def _DriverVer(self, a, s):
        return DRIVER_VERSION

    def GetRootDir(self, a, s):
        root = str(self.lifecycle.root_dir)
        try:
            return root.encode("mbcs")  # Dice feeds this to std::filesystem::path (ANSI code page)
        except UnicodeEncodeError:
            log.error("root_dir %r is not representable in the ANSI code page; Dice will fail to use it", root)
            return root.encode("utf-8")

    def Reload(self, a, s):
        log.warning("Reload requested by Dice: performing a process restart (see docs section 3.3)")
        self.lifecycle.restart("reload")
        return True

    def Remake(self, a, s):
        log.warning("Remake requested by Dice: restarting the driver process")
        self.lifecycle.restart("remake")
        return True

    def Killme(self, a, s):
        log.warning("Killme requested by Dice: exiting")
        self.lifecycle.exit()

    # ------------------------------------------------------------------ B. log / notify / heartbeat

    def DebugLog(self, a, s):
        dice_log.info(s)

    def DebugMsg(self, a, s):
        self.notifier.push(s)

    def DiceHeartbeat(self, a, s):
        self.heartbeat.push(s)

    def GetTinyID(self, a, s):
        log.debug("GetTinyID: guild support is off for OneBot 11, returning 0")
        return 0

    # ------------------------------------------------------------------ C. update (lazy)

    def DiceUpdate(self, a, s):
        log.info("DiceUpdate(ver=%s) called: handled by the build pipeline, official update skipped", s)
        return True, "更新由构建流程接管"

    # ------------------------------------------------------------------ D. identity / friends

    def GetNick(self, a, s):
        return self.state.nick(a[1], self._self_id)

    def IsFriend(self, a, s):
        _, qq, default = a
        table = self.state.friends()
        if table is None:
            return bool(default)
        return qq in table

    def GetFriendQQList(self, a, s):
        return set(self.state.friends() or {})

    # ------------------------------------------------------------------ E. sending

    def SendPrivateMsg(self, a, s):
        _, to = a
        if not s:
            return
        self.bot.call_nowait("send_private_msg", {"user_id": to, "message": self._pack_message(s)},
                             f"SendPrivateMsg(to={to}, len={len(s)})")

    def SendGroupMsg(self, a, s):
        _, to = a
        if not s:
            return
        self.bot.call_nowait("send_group_msg", {"group_id": to, "message": self._pack_message(s)},
                             f"SendGroupMsg(to={to}, len={len(s)})")

    # ------------------------------------------------------------------ F. group queries

    def GetGroupIDList(self, a, s):
        return self.state.group_ids()

    def GetGroupMemberList(self, a, s):
        table = self.state.members(a[1])
        if table is None:
            log.warning("GetGroupMemberList(%d): no data available, returning an empty set", a[1])
            return set()
        return set(table)

    def GetGroupAdminList(self, a, s):
        table = self.state.members(a[1])
        if table is None:
            log.warning("GetGroupAdminList(%d): no data available, returning an empty set", a[1])
            return set()
        return {uid for uid, m in table.items() if m.role in ("admin", "owner")}

    def GetGroupAuth(self, a, s):
        m = self.state.lookup(a[1], a[2])
        return m.auth if isinstance(m, Member) else 0

    def IsGroupAdmin(self, a, s):
        return self._role_check(a, ("admin", "owner"))

    def IsGroupOwner(self, a, s):
        return self._role_check(a, ("owner",))

    def IsGroupMember(self, a, s):
        return self._role_check(a, None)

    def _role_check(self, a, roles) -> bool:
        _, gid, uid, default = a
        m = self.state.lookup(gid, uid)
        if m is UNKNOWN:
            return bool(default)
        if m is None:
            return False
        return roles is None or m.role in roles

    def GetGroupName(self, a, s):
        info = self.state.group_info(a[1])
        return info.name if info else ""

    def GetGroupSize(self, a, s):
        info = self.state.group_info(a[1])
        curr = info.member_count if info else 0
        maxc = info.max_member_count if info else 0
        return ((curr & 0xFFFFFFFF) << 32) | (maxc & 0xFFFFFFFF)

    def GetGroupNick(self, a, s):
        m = self.state.lookup(a[1], a[2])
        return (m.display or "群员") if isinstance(m, Member) else "群员"

    def GetGroupLastMsg(self, a, s):
        return self.state.last_speak(a[1], a[2])

    def PrintGroupInfo(self, a, s):
        gid = a[1]
        info = self.state.group_info(gid)
        name, curr, maxc = (info.name, info.member_count, info.max_member_count) if info else ("", 0, 0)
        return f"[{name}]({gid})[{curr}/{maxc}]"

    # ------------------------------------------------------------------ G. group actions

    def SetGroupKick(self, a, s):
        _, gid, uid = a
        self.bot.call_nowait("set_group_kick", {"group_id": gid, "user_id": uid, "reject_add_request": False},
                             f"SetGroupKick({gid}, {uid})")
        self.state.invalidate_members(gid)

    def SetGroupBan(self, a, s):
        _, gid, uid, sec = a
        self.bot.call_nowait("set_group_ban", {"group_id": gid, "user_id": uid, "duration": max(sec, 0)},
                             f"SetGroupBan({gid}, {uid}, {sec}s)")

    def SetGroupAdmin(self, a, s):
        _, gid, uid, enable = a
        self.bot.call_nowait("set_group_admin", {"group_id": gid, "user_id": uid, "enable": bool(enable)},
                             f"SetGroupAdmin({gid}, {uid}, {bool(enable)})")
        self.state.invalidate_members(gid)

    def SetGroupCard(self, a, s):
        _, gid, uid = a
        self.bot.call_nowait("set_group_card", {"group_id": gid, "user_id": uid, "card": s},
                             f"SetGroupCard({gid}, {uid})")
        self.state.invalidate_members(gid)

    def SetGroupTitle(self, a, s):
        _, gid, uid = a
        self.bot.call_nowait("set_group_special_title",
                             {"group_id": gid, "user_id": uid, "special_title": s, "duration": -1},
                             f"SetGroupTitle({gid}, {uid})")

    def SetGroupWholeBan(self, a, s):
        _, gid, sec = a
        if sec > 0:
            self.state.set_whole_ban_expiry(gid, self.state.now() + sec)
            self.bot.call_nowait("set_group_whole_ban", {"group_id": gid, "enable": True},
                                 f"SetGroupWholeBan({gid}, {sec}s)")
        else:
            self.state.set_whole_ban_expiry(gid, None)
            self.bot.call_nowait("set_group_whole_ban", {"group_id": gid, "enable": False},
                                 f"SetGroupWholeBan({gid}, lift)")

    def SetGroupLeave(self, a, s):
        gid = a[1]
        self.bot.call_nowait("set_group_leave", {"group_id": gid, "is_dismiss": False}, f"SetGroupLeave({gid})")
        self.state.group_removed(gid)

    def SetDiscussLeave(self, a, s):
        self._warn_unsupported(f"SetDiscussLeave({a[1]}) (discussion groups)")

    def AnswerFriendRequest(self, a, s):
        _, uid, resp = a
        flag = self.state.pop_friend_flag(uid)
        if flag is None:
            log.warning("AnswerFriendRequest(%d, %d): no pending request flag (expired or never seen); dropped",
                        uid, resp)
            return
        approve = resp == 1
        self.bot.call_nowait("set_friend_add_request", {"flag": flag.flag, "approve": approve, "remark": ""},
                             f"AnswerFriendRequest({uid}, {'accept' if approve else 'reject'})")
        if approve and s and self.cfg.onebot.friend_greeting:
            threading.Timer(2.0, lambda: self.SendPrivateMsg((self._self_id, uid), s)).start()
        elif s:
            log.debug("friend request reply text not delivered: %r", s)

    def AnswerGroupInvited(self, a, s):
        _, gid, resp = a
        flag = self.state.pop_group_flag(gid)
        if flag is None:
            log.warning("AnswerGroupInvited(%d, %d): no pending invite flag (expired or never seen); dropped",
                        gid, resp)
            return
        approve = resp == 1  # 2 = reject, 3 = ignore (OneBot cannot ignore, so it is a reject)
        self.bot.call_nowait("set_group_add_request",
                             {"flag": flag.flag, "sub_type": flag.sub_type or "invite", "approve": approve,
                              "reason": "" if approve else "拒绝"},
                             f"AnswerGroupInvited({gid}, {'accept' if approve else 'reject'}, resp={resp})")

    # ------------------------------------------------------------------ H. files

    def UploadGroupFile(self, a, s):
        _, gid = a
        path = self._resolve_path(s)
        if not path.is_file():
            log.error("UploadGroupFile(%d): file not found: %s", gid, path)
            return True  # decision 14: no fallback to private chat
        self.bot.call_nowait("upload_group_file", {"group_id": gid, "file": str(path), "name": path.name},
                             f"UploadGroupFile({gid}, {path.name})", self.cfg.onebot.upload_timeout_s)
        return True

    def SendPrivateFile(self, a, s):
        _, uid = a
        path = self._resolve_path(s)
        if not path.is_file():
            log.error("SendPrivateFile(%d): file not found: %s", uid, path)
            return False
        try:
            self.bot.call_sync("upload_private_file", {"user_id": uid, "file": str(path), "name": path.name},
                               self.cfg.onebot.upload_timeout_s)
        except Exception as e:
            log.error("SendPrivateFile(%d, %s) failed: %s: %s", uid, path.name, type(e).__name__, e)
            return False
        return True
