"""OneBot 11 events -> Dice event exports (docs section 7). Handlers run on worker threads."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Executor
from typing import Callable

from .cq import message_to_cq
from .native import DiceNative, clamp_int32
from .state import State

log = logging.getLogger("dd.events")


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
            log.debug("private message from %d (len=%d)", uid, len(text))
            self.native.emit("eventPrivateMsg", msg_id, uid, text)
        elif kind == "group":
            gid = int(ev["group_id"])
            log.debug("group message in %d from %d (len=%d)", gid, uid, len(text))
            if gid not in self.state.group_ids():
                self.state.group_added(gid)
            self.state.note_speak(gid, uid, int(ev.get("time") or 0))
            self.native.emit("eventGroupMsg", msg_id, gid, uid, text)
        else:
            log.debug("ignored message_type %r", kind)

    def _notice(self, ev: dict) -> None:
        kind = ev.get("notice_type")
        sub = ev.get("sub_type", "")
        gid = int(ev.get("group_id") or 0)
        uid = int(ev.get("user_id") or 0)
        operator = int(ev.get("operator_id") or 0)
        me = self.self_id()
        log.debug("notice %s/%s group=%s user=%s operator=%s", kind, sub, gid, uid, operator)
        if kind == "group_increase":
            if uid == me:
                self.state.group_added(gid)
            else:
                self.state.invalidate_members(gid)
            self.native.emit("eventGroupMemberAdd", gid, uid, operator)
        elif kind == "group_decrease":
            if uid == me:
                self.state.group_removed(gid)
            else:
                self.state.invalidate_members(gid)
            if sub in ("kick", "kick_me"):
                self.native.emit("eventGroupMemberKicked", gid, uid, operator)
        elif kind == "group_ban":
            if not uid:
                log.info("whole-group ban notice in %d (%s); no Dice event for it", gid, sub)
                return
            duration = str(int(ev.get("duration") or 0)) if sub == "ban" else ""
            self.native.emit("eventGroupBan", gid, uid, operator, duration)
        elif kind == "friend_add":
            self.state.invalidate_friends()
            self.native.emit("eventFriendAdd", uid)
        elif kind in ("group_admin", "group_card"):
            self.state.invalidate_members(gid)
        else:
            log.debug("notice %s ignored", kind)

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
