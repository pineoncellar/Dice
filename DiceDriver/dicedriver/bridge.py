"""OneBot relay endpoint (中转): a second, independent OB11 face for an external bot.

An external framework (麦bot) connects here as if the driver were its protocol端. Actions are
proxied upstream under the driver's own echo namespace and the upstream's response frame is
handed back under the *caller's* echo; events are relayed downstream.

The only thing this relay filters is **message send/receive**: while Dice is recording a
chat (`.log on`, i.e. a TRPG session in progress) group messages for that group are neither
relayed down nor accepted up. Every `get_*` query and every other action passes through
untouched — the relay must not constrain what the external bot is able to do.

The upstream link and this relay keep separate connections, separate echo namespaces and
separate pending maps, so they can never displace each other.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import serve

from . import logs
from .onebot import LinkDown, OneBotLink

log = logging.getLogger("dd.bridge")

# Dice's TRPG-state report (see DiceSrc/docs/DiceDriver-interface-duties.md §4.4).
LOGSTATE_MARK = "@dice.logstate"
_SNAPSHOT = "snapshot"
_SNAPSHOT_END = "snapshot-end"

# Actions that carry a chat message. Only these are ever gated; anything else is a pass-through.
_GROUP_SEND = frozenset({"send_group_msg", "send_group_forward_msg"})
_PRIVATE_SEND = frozenset({"send_private_msg"})
_GROUP_EVENT = "group"
_PRIVATE_EVENT = "private"

# OB11-ish retcodes for locally rejected requests (the spec only fixes 0).
RET_BAD_REQUEST = 1400
RET_BLOCKED = 1404
RET_UPSTREAM = 1401


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _chat_key(gid: Any = 0, uid: Any = 0, chid: Any = 0) -> tuple[str, int] | None:
    """One chat window, normalised so config strings and JSON numbers compare equal."""
    if (n := _as_int(gid)):
        return ("g", n)
    if (n := _as_int(uid)):
        return ("u", n)
    if (n := _as_int(chid)):
        return ("c", n)
    return None


class LogGate:
    """Which chat windows Dice is recording, fed by the `@dice.logstate` lines.

    Authoritative-snapshot model. Dice emits one line per recording window followed by a
    `snapshot-end`; that terminator replaces the whole table, so a window absent from it is
    known *not* to be recording. Until the first snapshot completes the state is unknown and
    `on_unknown` decides — with `snapshot_grace_s` as the escape hatch, so that a Dice build
    which never reports cannot silence the external bot forever.
    """

    def __init__(self, on_unknown: str = "closed", grace_s: float = 30.0):
        self._lock = threading.Lock()
        self._on: set[tuple[str, int]] = set()
        self._seen: set[tuple[str, int]] = set()
        self._bootstrapped = False
        self._on_unknown = on_unknown
        self._grace_s = grace_s
        self._armed_at = 0.0
        self._warned = False

    # ------------------------------------------------------------------ input

    def arm(self) -> None:
        """Start the clock for the snapshot grace period (called as Dice boots)."""
        with self._lock:
            self._armed_at = time.monotonic()

    def feed(self, line: str) -> bool:
        """Consume one DebugLog line. Returns True when it was a state report."""
        info = self.parse(line)
        if info is None:
            return False
        action = info.get("action", "")
        key = _chat_key(info.get("gid"), info.get("uid"), info.get("chid"))
        with self._lock:
            if action == _SNAPSHOT:
                if key is not None:
                    self._seen.add(key)
            elif action == _SNAPSHOT_END:
                self._on = self._seen
                self._seen = set()
                self._bootstrapped = True
            elif key is not None:
                if info.get("on") == "1":
                    self._on.add(key)
                else:
                    self._on.discard(key)
                # An incremental report proves the channel works, but it is not the full
                # picture, so it must not promote the gate to "everyone else is idle".
        logs.trace(log, "logstate %s -> %s", action or "?", self.describe())
        return True

    @staticmethod
    def parse(line: str) -> dict[str, str] | None:
        """`@dice.logstate v1 on=1 action=new ... name=<may contain spaces>` -> dict."""
        if not line.startswith(LOGSTATE_MARK + " "):
            return None
        parts = line[len(LOGSTATE_MARK) + 1:].split(" ")
        if not parts or parts[0] != "v1":
            return None
        out: dict[str, str] = {}
        for i in range(1, len(parts)):
            key, sep, value = parts[i].partition("=")
            if not sep:
                continue
            if key == "name":  # always last, and may contain spaces
                out[key] = " ".join([value, *parts[i + 1:]])
                break
            out[key] = value
        return out

    # ------------------------------------------------------------------ query

    def gated(self, key: tuple[str, int] | None) -> bool:
        """True when this chat must have its messages withheld."""
        if key is None:
            return False
        with self._lock:
            if self._bootstrapped:
                return key in self._on
            if self._on_unknown == "open":
                return False
            if self._armed_at and time.monotonic() - self._armed_at >= self._grace_s:
                if not self._warned:
                    self._warned = True
                    log.warning("no %s report within %.0fs; assuming no TRPG session is running "
                                "(check that the Dice DLL emits it)", LOGSTATE_MARK, self._grace_s)
                return False
            return True

    def describe(self) -> str:
        with self._lock:
            state = "snapshot" if self._bootstrapped else "unknown"
            return f"{state}, {len(self._on)} recording: {sorted(self._on)}"


class RelayClient:
    """One connected external bot, with a bounded outbound queue so a slow reader cannot stall us."""

    def __init__(self, ws, limit: int):
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self._limit = limit
        self._dropped = 0

    def offer(self, text: str) -> bool:
        """Event path: never block, drop when the client is too slow."""
        if self.closed:
            return False
        if self.queue.qsize() >= self._limit:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                log.warning("relay client is behind; dropped %d event(s)", self._dropped)
            return False
        self.queue.put_nowait(text)
        return True

    async def put(self, text: str) -> None:
        """Response path: apply back-pressure instead of dropping an answer."""
        if not self.closed:
            await self.queue.put(text)

    async def pump(self) -> None:
        while True:
            text = await self.queue.get()
            if text is None:
                return
            await self.ws.send(text)

    def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


def _reply(echo: Any, retcode: int, wording: str) -> str:
    return json.dumps({"status": "failed", "retcode": retcode, "data": None,
                       "wording": wording, "echo": echo}, ensure_ascii=False)


class RelayBridge:
    """The relay server. Owns its own listening socket and client set."""

    def __init__(self, cfg, link: OneBotLink, gate: LogGate):
        self.cfg = cfg
        self.link = link
        self.gate = gate
        self._clients: list[RelayClient] = []
        self._tasks: set[asyncio.Task] = set()  # keep refs: a bare task can be GC'd mid-flight
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread = 0
        self._stop: asyncio.Event | None = None

    @property
    def clients(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._stop = asyncio.Event()
        log.info("relay listening for OneBot clients on ws://%s:%d%s",
                 self.cfg.host, self.cfg.port, self.cfg.path)
        async with serve(self._handler, self.cfg.host, self.cfg.port,
                         process_request=self._authorize, max_size=None,
                         ping_interval=20, ping_timeout=20):
            await self._stop.wait()

    async def close(self) -> None:
        if self._stop:
            self._stop.set()
        for client in list(self._clients):
            try:
                await client.ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ downstream

    def relay_event(self, ev: dict) -> None:
        """Fan one upstream event out to the relay clients (called on the event-loop thread)."""
        if self._loop is not None and threading.get_ident() != self._loop_thread:
            self._loop.call_soon_threadsafe(self.relay_event, ev)
            return
        if not self._clients:
            return
        reason = self._withheld(ev)
        if reason:
            logs.trace(log, "relay withheld %s (%s)", ev.get("post_type"), reason)
            return
        text = json.dumps(ev, ensure_ascii=False)
        for client in list(self._clients):
            client.offer(text)

    def _withheld(self, ev: dict) -> str:
        """Why this event must not reach the external bot ('' = relay it)."""
        post_type = ev.get("post_type")
        if post_type not in ("message", "message_sent"):
            return ""
        if post_type == "message_sent" and not self.cfg.relay_self_messages:
            # Both bots share one account, so we cannot tell whose message this is; relaying
            # it would let the external bot answer the driver's own replies (and itself).
            return "message_sent"
        kind = ev.get("message_type")
        if kind == _GROUP_EVENT:
            return "TRPG session" if self.gate.gated(_chat_key(gid=ev.get("group_id"))) else ""
        if self.cfg.gate_private and kind == _PRIVATE_EVENT:
            return "TRPG session" if self.gate.gated(_chat_key(uid=ev.get("user_id"))) else ""
        return ""

    # ------------------------------------------------------------------ upstream

    def target_of(self, action: str, params: dict) -> tuple[str, int] | None:
        """The chat an action would post a message to, if it posts one at all."""
        if action in _GROUP_SEND:
            return _chat_key(gid=params.get("group_id"))
        if action in _PRIVATE_SEND:
            return _chat_key(uid=params.get("user_id")) if self.cfg.gate_private else None
        if action == "send_msg":
            if _as_int(params.get("group_id")) or str(params.get("message_type", "")).lower() == _GROUP_EVENT:
                return _chat_key(gid=params.get("group_id"))
            return _chat_key(uid=params.get("user_id")) if self.cfg.gate_private else None
        return None

    async def _action(self, client: RelayClient, req: dict) -> None:
        action = str(req.get("action") or "")
        params = req.get("params") or {}
        echo = req.get("echo")
        if not isinstance(params, dict):
            await client.put(_reply(echo, RET_BAD_REQUEST, "params must be an object"))
            return
        if not action:
            await client.put(_reply(echo, RET_BAD_REQUEST, "missing action"))
            return
        target = self.target_of(action, params)
        if target is not None and self.gate.gated(target):
            log.info("relay blocked %s to %s: in a TRPG session", action, target)
            await client.put(_reply(echo, RET_BLOCKED, "该聊天正在跑团（日志记录中），消息已拦截"))
            return
        if not self.link.connected:
            log.warning("relay dropping %s: upstream OneBot link is down", action)
            await client.put(_reply(echo, RET_UPSTREAM, "upstream OneBot connection is down"))
            return
        timeout = self.link.cfg.upload_timeout_s if action.startswith("upload_") else None
        try:
            resp = await self.link.call_raw(action, params, timeout)
        except TimeoutError:
            await client.put(_reply(echo, RET_UPSTREAM, f"{action} timed out upstream"))
            return
        except LinkDown:
            await client.put(_reply(echo, RET_UPSTREAM, "upstream OneBot connection is down"))
            return
        except Exception as e:  # never let one request break the relay
            log.exception("relay %s failed", action)
            await client.put(_reply(echo, RET_UPSTREAM, f"{type(e).__name__}: {e}"))
            return
        out = dict(resp)
        out["echo"] = echo  # restore the caller's echo; ours is an internal detail
        logs.trace(log, "relay %s -> retcode=%s", action, out.get("retcode"))
        await client.put(json.dumps(out, ensure_ascii=False))

    # ------------------------------------------------------------------ server plumbing

    def _authorize(self, connection, request):
        parts = urlsplit(request.path)
        if parts.path.rstrip("/") != self.cfg.path.rstrip("/"):
            log.warning("relay: rejected path %s", parts.path)
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        token = self.cfg.access_token
        if token:
            auth = request.headers.get("Authorization", "")
            query = parse_qs(parts.query).get("access_token", [""])[0]
            if auth not in (f"Bearer {token}", f"Token {token}") and query != token:
                log.warning("relay: rejected connection (bad access token)")
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def _handler(self, ws) -> None:
        client = RelayClient(ws, self.cfg.queue_limit)
        self._clients.append(client)
        log.info("relay client connected from %s (%d total)", ws.remote_address, len(self._clients))
        pump = asyncio.create_task(client.pump())
        try:
            async for raw in ws:
                try:
                    req = json.loads(raw)
                except ValueError:
                    log.warning("relay: ignored non-JSON frame (%d bytes)", len(raw))
                    continue
                if not isinstance(req, dict):
                    continue
                logs.trace(log, "relay< %s %s", req.get("action"), req.get("params"))
                task = asyncio.create_task(self._action(client, req))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("relay client handler failed")
        finally:
            client.close()
            pump.cancel()
            if client in self._clients:
                self._clients.remove(client)
            log.info("relay client disconnected (%d left)", len(self._clients))
