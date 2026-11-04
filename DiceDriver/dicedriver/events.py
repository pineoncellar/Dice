"""OneBot 11 events -> Dice event exports (docs section 7). Handlers run on worker threads.

The workers do the bookkeeping; the calls into the DLL itself are serialised in native.py, because
Dice writes global state without locks and must only ever be entered from one thread at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Executor
from typing import Callable

from . import logs
from .cq import message_to_cq
from .native import DiceNative, clamp_int32
from .state import State

log = logging.getLogger("dd.events")

# Dice 判断"这是不是一条指令"的方式见 DiceSrc/Dice/MsgFormat.cpp 的 strip()：反复剥掉这些前缀。
_COMMAND_PREFIXES = (".", "!", "。", "！", "．")

# 指令原文进日志，但超长的（属性批量导入那种几百字）只留一个能认出来的头。
_CMD_LOG_LIMIT = 300


def command_of(text: str) -> str | None:
    """指令原文；不是指令则返回 None。非指令的内容一律不写日志（隐私）。"""
    stripped = text.lstrip()
    return stripped if stripped[:1] in _COMMAND_PREFIXES else None


def _clip(text: str) -> str:
    if len(text) <= _CMD_LOG_LIMIT:
        return text
    return f"{text[:_CMD_LOG_LIMIT]}…(+{len(text) - _CMD_LOG_LIMIT}字)"


class EventRouter:
    def __init__(self, cfg, state: State, native: DiceNative, self_id: Callable[[], int],
                 executor: Executor, is_ready: Callable[[], bool]):
        self.cfg = cfg
        self.state = state
        self.native = native
        self.self_id = self_id
        self.executor = executor
        self.is_ready = is_ready
        self._tails: dict[tuple, asyncio.Task] = {}

    # ------------------------------------------------------------------ loop-thread entry

    def on_event(self, ev: dict) -> None:
        """Called on the event loop for every OneBot event; hands work to the pool, per chat in order."""
        post_type = ev.get("post_type")
        if post_type == "meta_event":
            log.debug("meta_event %s/%s", ev.get("meta_event_type"), ev.get("sub_type", ""))
            return
        if not self.is_ready():
            log.debug("event dropped, Dice not started yet: %s/%s", post_type,
                      ev.get(f"{post_type}_type"))
            return
        key = ("g", ev["group_id"]) if ev.get("group_id") else ("u", ev.get("user_id", 0))
        loop = asyncio.get_running_loop()
        prev = self._tails.get(key)

        async def run() -> None:
            if prev is not None:
                try:
                    await prev
                except BaseException:
                    pass
            try:
                await loop.run_in_executor(self.executor, self.handle, ev)
            except Exception:
                log.exception("handler for %s event failed: %s", post_type, self._brief(ev))

        task = loop.create_task(run())
        self._tails[key] = task
        task.add_done_callback(lambda t, k=key: self._tails.pop(k, None) if self._tails.get(k) is t else None)

    @staticmethod
    def _brief(ev: dict) -> str:
        return str({k: v for k, v in ev.items() if k not in ("message", "raw_message")})[:300]

    # ------------------------------------------------------------------ worker-thread handlers

    def handle(self, ev: dict) -> None:
        post_type = ev.get("post_type")
        if post_type in ("message", "message_sent"):
            self._message(ev)
        elif post_type == "notice":
            self._notice(ev)
        elif post_type == "request":
            self._request(ev)
        else:
            log.debug("ignored post_type %r", post_type)

    def _message(self, ev: dict) -> None:
        uid = int(ev.get("user_id", 0))
        is_self = ev.get("post_type") == "message_sent" or uid == self.self_id()
        if is_self and not self.cfg.onebot.forward_self_messages:
            return
        text = message_to_cq(ev.get("message"), ev.get("raw_message"))
        msg_id = clamp_int32(int(ev.get("message_id", 0)))
        kind = ev.get("message_type")
        if kind == "private":
            self._deliver("eventPrivateMsg", "私聊", uid, msg_id, text, (msg_id, uid, text))
        elif kind == "group":
            gid = int(ev["group_id"])
            if gid not in self.state.group_ids():
                self.state.group_added(gid)
            self.state.note_speak(gid, uid, int(ev.get("time") or 0))
            self._deliver("eventGroupMsg", f"群{gid}", uid, msg_id, text, (msg_id, gid, uid, text))
        else:
            log.debug("忽略 message_type=%r", kind)

    def _deliver(self, name: str, where: str, uid: int, msg_id: int, text: str, args: tuple) -> None:
        """把一条消息交给 Dice，并记下"它是什么"与"处理出了什么结果"。

        指令原文进日志——否则事后没人分得清"指令失败"和"骰娘卡死"；普通聊天内容不进日志，
        只留定位用的元信息，这样日志里不会残留别人的聊天记录。
        """
        cmd = command_of(text)
        sent = self.native.api.sends
        started = time.monotonic()
        if cmd is None:
            ret = self.native.emit(name, *args)
            logs.trace(log, "%s ← 非指令 uid=%d msgid=%d len=%d ret=%d 用时 %.0fms（内容不记录）",
                       where, uid, msg_id, len(text), ret, (time.monotonic() - started) * 1000)
            return
        shown = repr(_clip(cmd))
        log.info("指令 %s | %s uid=%d msgid=%d", shown, where, uid, msg_id)
        ret = self.native.emit(name, *args)
        log.info("指令完成 %s | ret=%d 用时 %.0fms 回复 %d 条", shown, ret,
                 (time.monotonic() - started) * 1000, self.native.api.sends - sent)

    def _notice(self, ev: dict) -> None:
        kind = ev.get("notice_type")
        sub = ev.get("sub_type", "")
        gid = int(ev.get("group_id") or 0)
        uid = int(ev.get("user_id") or 0)
        operator = int(ev.get("operator_id") or 0)
        me = self.self_id()
        where = f"{kind}/{sub if sub else '-'} 群={gid} 用户={uid} 操作者={operator}"
        if kind == "group_increase":
            if uid == me:
                self.state.group_added(gid)
            else:
                self.state.invalidate_members(gid)
            self.native.emit("eventGroupMemberAdd", gid, uid, operator)
            done = "Dice: eventGroupMemberAdd"
        elif kind == "group_decrease":
            if uid == me:
                self.state.group_removed(gid)
            else:
                self.state.invalidate_members(gid)
            if sub in ("kick", "kick_me"):
                self.native.emit("eventGroupMemberKicked", gid, uid, operator)
                done = "Dice: eventGroupMemberKicked"
            else:
                done = "仅更新成员缓存"
        elif kind == "group_ban":
            if not uid:
                log.info("通知 %s → 全员禁言，Dice 无对应事件", where)
                return
            duration = str(int(ev.get("duration") or 0)) if sub == "ban" else ""
            self.native.emit("eventGroupBan", gid, uid, operator, duration)
            done = "Dice: eventGroupBan"
        elif kind == "friend_add":
            self.state.invalidate_friends()
            self.native.emit("eventFriendAdd", uid)
            done = "Dice: eventFriendAdd"
        elif kind in ("group_admin", "group_card"):
            self.state.invalidate_members(gid)
            done = "仅更新成员缓存"
        else:
            done = "忽略"
        log.debug("通知 %s → %s", where, done)

    def _request(self, ev: dict) -> None:
        kind = ev.get("request_type")
        uid = int(ev.get("user_id") or 0)
        flag = str(ev.get("flag", ""))
        if kind == "friend":
            log.info("friend request from %d (flag cached)", uid)
            self.state.put_friend_flag(uid, flag)
            self.native.emit("eventFriendRequest", uid, ev.get("comment", "") or "")
        elif kind == "group":
            sub = ev.get("sub_type", "")
            gid = int(ev.get("group_id") or 0)
            if sub != "invite":
                log.debug("group join request (%s) in %d ignored; the bot only handles invitations", sub, gid)
                return
            log.info("group invitation to %d from %d (flag cached)", gid, uid)
            self.state.put_group_flag(gid, flag, sub)
            self.native.emit("eventGroupInvited", gid, uid)
        else:
            log.debug("request %s ignored", kind)
