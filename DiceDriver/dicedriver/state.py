"""Caches and persistent state behind the host API.

Everything here is called from Dice / worker threads (never from the event loop), so it may block
on `bot.call_sync`. Rules from docs/DiceDriver-interface-duties.md section 3.2:
  member list TTL 60s, keep the last good value on failure; nick TTL 10min; friends TTL 5min;
  group list trusts events plus a periodic full refresh; last-speak table in memory, flushed to disk;
  whole-ban expiry persisted; stale invite/friend flags are logged and dropped.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("dd.state")

ROLE_AUTH = {"member": 1, "admin": 2, "owner": 3}


class _Unknown:
    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()  # "could not find out" as opposed to a definite "not a member"


@dataclass
class Member:
    user_id: int
    role: str = "member"
    card: str = ""
    nickname: str = ""
    last_sent: int = 0

    @property
    def auth(self) -> int:
        return ROLE_AUTH.get(self.role, 1)

    @property
    def display(self) -> str:
        return self.card or self.nickname


@dataclass
class GroupInfo:
    group_id: int
    name: str = ""
    member_count: int = 0
    max_member_count: int = 0


@dataclass
class _Timed:
    value: object = None
    ts: float = 0.0


@dataclass
class Flag:
    flag: str
    ts: float
    sub_type: str = ""


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("state file %s unreadable, starting empty: %s", path, e)
        return {}


class State:
    def __init__(self, bot, cache_cfg, state_dir: Path, clock: Callable[[], float] = time.time):
        self.bot = bot
        self.cfg = cache_cfg
        self.dir = state_dir
        self.now = clock
        self._lock = threading.RLock()

        self._friends = _Timed()  # value: dict[int, str]
        self._friend_lock = threading.Lock()
        self._groups: dict[int, GroupInfo] = {}
        self._groups_loaded = False
        self._groups_ts = 0.0
        self._group_lock = threading.Lock()
        self._members: dict[int, _Timed] = {}  # value: dict[int, Member]
        self._member_locks: dict[int, threading.Lock] = {}
        self._single: dict[tuple[int, int], tuple[Member | None, float]] = {}
        self._nicks: dict[int, tuple[str, float]] = {}

        self._last_speak: dict[int, dict[int, int]] = {}
        self._last_speak_dirty = False
        self._last_flush = self.now()
        self._whole_ban: dict[int, float] = {}
        self._friend_flags: dict[int, Flag] = {}
        self._group_flags: dict[int, Flag] = {}
        self._last_group_refresh = self.now()

        self._load_persistent()

    # ------------------------------------------------------------------ persistence

    def _load_persistent(self) -> None:
        raw = _read_json(self.dir / "last_speak.json")
        for gid, users in raw.items():
            self._last_speak[int(gid)] = {int(u): int(t) for u, t in users.items()}
        self._whole_ban = {int(g): float(t) for g, t in _read_json(self.dir / "whole_ban.json").items()}
        log.info("state loaded: last_speak groups=%d, pending whole-bans=%d",
                 len(self._last_speak), len(self._whole_ban))

    def flush(self, force: bool = False) -> None:
        with self._lock:
            if not (self._last_speak_dirty or force):
                return
            snapshot = {str(g): {str(u): t for u, t in users.items()} for g, users in self._last_speak.items()}
            self._last_speak_dirty = False
            self._last_flush = self.now()
        try:
            _atomic_write(self.dir / "last_speak.json", snapshot)
        except OSError as e:
            log.error("failed to write last_speak.json: %s", e)
            self._last_speak_dirty = True

    def _save_whole_ban(self) -> None:
        with self._lock:
            snapshot = {str(g): t for g, t in self._whole_ban.items()}
        try:
            _atomic_write(self.dir / "whole_ban.json", snapshot)
        except OSError as e:
            log.error("failed to write whole_ban.json: %s", e)

    # ------------------------------------------------------------------ friends / nicks

    def friends(self) -> dict[int, str] | None:
        """uid -> nickname, or None if it was never obtainable."""
        with self._friend_lock:
            if self._friends.value is not None and self.now() - self._friends.ts < self.cfg.friend_ttl_s:
                return self._friends.value
            try:
                data = self.bot.call_sync("get_friend_list")
                self._friends = _Timed({int(f["user_id"]): f.get("remark") or f.get("nickname", "")
                                        for f in data or []}, self.now())
                log.debug("friend list refreshed: %d", len(self._friends.value))
            except Exception as e:
                log.warning("get_friend_list failed (%s: %s); %s", type(e).__name__, e,
                            "keeping previous list" if self._friends.value is not None else "no data")
            return self._friends.value

    def invalidate_friends(self) -> None:
        with self._friend_lock:
            self._friends.ts = 0.0

    def nick(self, qq: int, self_id: int) -> str:
        with self._lock:
            hit = self._nicks.get(qq)
            if hit and self.now() - hit[1] < self.cfg.nick_ttl_s:
                return hit[0]
        old = hit[0] if hit else ""
        try:
            if qq == self_id:
                data = self.bot.call_sync("get_login_info")
            else:
                data = self.bot.call_sync("get_stranger_info", {"user_id": qq})
            name = str((data or {}).get("nickname", ""))
        except Exception as e:
            log.warning("nickname lookup for %d failed (%s: %s)", qq, type(e).__name__, e)
            name = old
        if not name:
            name = old
        with self._lock:
            self._nicks[qq] = (name, self.now())
        return name

    # ------------------------------------------------------------------ groups

    def refresh_groups(self) -> bool:
        with self._group_lock:
            try:
                data = self.bot.call_sync("get_group_list")
            except Exception as e:
                log.warning("get_group_list failed (%s: %s); keeping %d known groups",
                            type(e).__name__, e, len(self._groups))
                return False
            fresh = {int(g["group_id"]): GroupInfo(int(g["group_id"]), g.get("group_name", ""),
                                                   int(g.get("member_count") or 0),
                                                   int(g.get("max_member_count") or 0)) for g in data or []}
            with self._lock:
                gone = set(self._groups) - set(fresh)
                self._groups = fresh
                self._groups_loaded = True
                self._groups_ts = self.now()
                for gid in gone:
                    self._forget_group_locked(gid)
            log.info("group list refreshed: %d groups (%d gone)", len(fresh), len(gone))
            return True

    def group_ids(self) -> set[int]:
        if not self._groups_loaded:
            self.refresh_groups()
        with self._lock:
            return set(self._groups)

    def group_info(self, gid: int) -> GroupInfo | None:
        with self._lock:
            info = self._groups.get(gid)
        if info is not None:
            return info
        try:
            d = self.bot.call_sync("get_group_info", {"group_id": gid})
        except Exception as e:
            log.warning("get_group_info(%d) failed (%s: %s)", gid, type(e).__name__, e)
            return None
        info = GroupInfo(gid, d.get("group_name", ""), int(d.get("member_count") or 0),
                         int(d.get("max_member_count") or 0))
        with self._lock:
            self._groups[gid] = info
        return info

    def group_added(self, gid: int) -> None:
        with self._lock:
            self._groups.setdefault(gid, GroupInfo(gid))
            self._groups_loaded = True
            self._members.pop(gid, None)
        log.info("group %d added", gid)

    def group_removed(self, gid: int) -> None:
        with self._lock:
            self._groups.pop(gid, None)
            self._forget_group_locked(gid)
        log.info("group %d removed", gid)

    def _forget_group_locked(self, gid: int) -> None:
        self._members.pop(gid, None)
        self._last_speak.pop(gid, None)
        self._last_speak_dirty = True
        for key in [k for k in self._single if k[0] == gid]:
            del self._single[key]
        if self._whole_ban.pop(gid, None) is not None:
            threading.Thread(target=self._save_whole_ban, daemon=True).start()

    # ------------------------------------------------------------------ members

    def _member_lock(self, gid: int) -> threading.Lock:
        with self._lock:
            return self._member_locks.setdefault(gid, threading.Lock())

    def members(self, gid: int) -> dict[int, Member] | None:
        with self._lock:
            entry = self._members.get(gid)
        if entry and self.now() - entry.ts < self.cfg.member_ttl_s:
            return entry.value
        with self._member_lock(gid):
            with self._lock:  # another thread may have refreshed while we waited
                entry = self._members.get(gid)
            if entry and self.now() - entry.ts < self.cfg.member_ttl_s:
                return entry.value
            try:
                data = self.bot.call_sync("get_group_member_list", {"group_id": gid})
                table = {int(m["user_id"]): Member(int(m["user_id"]), m.get("role", "member"),
                                                   m.get("card", "") or "", m.get("nickname", "") or "",
                                                   int(m.get("last_sent_time") or 0)) for m in data or []}
                with self._lock:
                    self._members[gid] = _Timed(table, self.now())
                    info = self._groups.get(gid)
                    if info is not None:
                        info.member_count = len(table)
                log.debug("member list of %d refreshed: %d members", gid, len(table))
                return table
            except Exception as e:
                log.warning("get_group_member_list(%d) failed (%s: %s); %s", gid, type(e).__name__, e,
                            "keeping previous list" if entry else "no data")
                return entry.value if entry else None

    def invalidate_members(self, gid: int) -> None:
        with self._lock:
            entry = self._members.get(gid)
            if entry:
                entry.ts = 0.0
            for key in [k for k in self._single if k[0] == gid]:
                del self._single[key]

    def lookup(self, gid: int, uid: int):
        """Member | None (definitely not in the group) | UNKNOWN (could not find out)."""
        table = self.members(gid)
        if table is not None:
            return table.get(uid)
        with self._lock:
            hit = self._single.get((gid, uid))
        if hit and self.now() - hit[1] < self.cfg.member_ttl_s:
            return hit[0]
        try:
            d = self.bot.call_sync("get_group_member_info", {"group_id": gid, "user_id": uid})
            m = Member(uid, d.get("role", "member"), d.get("card", "") or "", d.get("nickname", "") or "",
                       int(d.get("last_sent_time") or 0))
        except Exception as e:
            log.warning("get_group_member_info(%d,%d) failed (%s: %s)", gid, uid, type(e).__name__, e)
            return UNKNOWN
        with self._lock:
            self._single[(gid, uid)] = (m, self.now())
        return m

    # ------------------------------------------------------------------ last speak

    def note_speak(self, gid: int, uid: int, ts: int) -> None:
        with self._lock:
            self._last_speak.setdefault(gid, {})[uid] = ts
            self._last_speak_dirty = True

    def last_speak(self, gid: int, uid: int) -> int:
        with self._lock:
            local = self._last_speak.get(gid, {}).get(uid, 0)
            entry = self._members.get(gid)
        remote = 0
        if entry:
            m = entry.value.get(uid)
            remote = m.last_sent if m else 0
        best = max(local, remote)
        return best if best > 0 else -1

    # ------------------------------------------------------------------ request flags

    def put_friend_flag(self, uid: int, flag: str) -> None:
        with self._lock:
            self._friend_flags[uid] = Flag(flag, self.now())

    def pop_friend_flag(self, uid: int) -> Flag | None:
        with self._lock:
            return self._friend_flags.pop(uid, None)

    def put_group_flag(self, gid: int, flag: str, sub_type: str) -> None:
        with self._lock:
            self._group_flags[gid] = Flag(flag, self.now(), sub_type)

    def pop_group_flag(self, gid: int) -> Flag | None:
        with self._lock:
            return self._group_flags.pop(gid, None)

    # ------------------------------------------------------------------ whole-group ban

    def set_whole_ban_expiry(self, gid: int, expires_at: float | None) -> None:
        with self._lock:
            if expires_at is None:
                self._whole_ban.pop(gid, None)
            else:
                self._whole_ban[gid] = expires_at
        self._save_whole_ban()

    def due_whole_bans(self) -> list[int]:
        with self._lock:
            now = self.now()
            return [g for g, t in self._whole_ban.items() if t <= now]

    # ------------------------------------------------------------------ periodic work

    def tick(self) -> None:
        """Called about once a second by the maintenance thread."""
        now = self.now()
        for gid in self.due_whole_bans():
            log.info("whole-group ban of %d expired, lifting", gid)
            try:
                self.bot.call_sync("set_group_whole_ban", {"group_id": gid, "enable": False})
                self.set_whole_ban_expiry(gid, None)
            except Exception as e:
                log.warning("lifting whole-ban of %d failed (%s: %s); will retry", gid, type(e).__name__, e)
        if self._last_speak_dirty and now - self._last_flush >= self.cfg.last_speak_flush_s:
            self.flush()
        if now - self._last_group_refresh >= self.cfg.group_refresh_s:
            self._last_group_refresh = now
            self.refresh_groups()
        with self._lock:
            for table, kind in ((self._friend_flags, "friend request"), (self._group_flags, "group invite")):
                for key in [k for k, f in table.items() if now - f.ts > self.cfg.flag_ttl_s]:
                    log.warning("%s flag of %d expired unanswered, dropped", kind, key)
                    del table[key]
