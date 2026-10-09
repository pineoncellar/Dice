"""OneBot 11 WebSocket link: forward (we connect) or reverse (we listen), with echo-matched actions."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from http import HTTPStatus
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from . import logs

log = logging.getLogger("dd.onebot")

EventHandler = Callable[[dict], None]
ConnectedHandler = Callable[[int], Awaitable[None]]
DisconnectedHandler = Callable[[], None]


class OneBotError(Exception):
    def __init__(self, action: str, retcode: int, message: str):
        super().__init__(f"{action} failed: retcode={retcode} {message}")
        self.action = action
        self.retcode = retcode
        self.message = message


class LinkDown(ConnectionError):
    pass


class OneBotLink:
    def __init__(self, cfg, on_event: EventHandler, on_connected: ConnectedHandler,
                 on_disconnected: DisconnectedHandler):
        self.cfg = cfg
        self._on_event = on_event
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._echo = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread = 0
        self._stop: asyncio.Event | None = None
        self.self_id = 0

    # ------------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._stop = asyncio.Event()
        if self.cfg.mode == "forward":
            await self._run_forward()
        else:
            await self._run_reverse()

    async def close(self) -> None:
        if self._stop:
            self._stop.set()
        ws = self._ws
        if ws is not None:
            await ws.close()

    # ------------------------------------------------------------------ forward

    async def _run_forward(self) -> None:
        headers = {}
        if self.cfg.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.access_token}"
        delay = self.cfg.reconnect_min_s
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                log.info("connecting to %s", self.cfg.url)
                async with connect(self.cfg.url, additional_headers=headers, max_size=None,
                                   ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                    log.info("forward websocket connected")
                    await self._session(ws, hint_self_id=0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("forward websocket error: %s: %s", type(e).__name__, e)
            if self._stop.is_set():
                break
            if time.monotonic() - started > 30:
                delay = self.cfg.reconnect_min_s
            log.warning("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self.cfg.reconnect_max_s)

    # ------------------------------------------------------------------ reverse

    def _authorize(self, connection, request):
        parts = urlsplit(request.path)
        if parts.path.rstrip("/") != self.cfg.path.rstrip("/"):
            log.warning("reverse: rejected path %s", parts.path)
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        token = self.cfg.access_token
        if token:
            auth = request.headers.get("Authorization", "")
            query = parse_qs(parts.query).get("access_token", [""])[0]
            if auth not in (f"Bearer {token}", f"Token {token}") and query != token:
                log.warning("reverse: rejected connection (bad access token)")
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def _reverse_handler(self, ws) -> None:
        hint = 0
        try:
            hint = int(ws.request.headers.get("X-Self-ID", "0"))
        except ValueError:
            pass
        log.info("reverse websocket connected from %s", ws.remote_address)
        try:
            await self._session(ws, hint_self_id=hint)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("reverse session error: %s: %s", type(e).__name__, e)

    async def _run_reverse(self) -> None:
        log.info("listening for reverse websocket on ws://%s:%d%s",
                 self.cfg.host, self.cfg.port, self.cfg.path)
        async with serve(self._reverse_handler, self.cfg.host, self.cfg.port,
                         process_request=self._authorize, max_size=None,
                         ping_interval=20, ping_timeout=20):
            await self._stop.wait()

    # ------------------------------------------------------------------ session

    async def _session(self, ws, hint_self_id: int) -> None:
        previous = self._ws
        self._ws = ws
        if previous is not None and previous is not ws:
            log.warning("a new connection replaces the previous one")
            await previous.close(1012, "replaced")
        reader = asyncio.create_task(self._reader(ws))
        try:
            self_id = hint_self_id
            if not self_id:
                info = await self.call("get_login_info", {}, self.cfg.call_timeout_s)
                self_id = int(info["user_id"])
            self.self_id = self_id
            await self._on_connected(self_id)
            await reader
        except Exception as e:
            log.error("session aborted: %s: %s", type(e).__name__, e)
            await ws.close()
        finally:
            reader.cancel()
            if self._ws is ws:
                self._ws = None
                self._fail_pending(LinkDown("connection closed"))
                try:
                    self._on_disconnected()
                except Exception:
                    log.exception("on_disconnected handler failed")
            log.warning("websocket disconnected")

    async def _reader(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except ValueError:
                    log.warning("ignored non-JSON frame (%d bytes)", len(raw))
                    continue
                if not isinstance(data, dict):
                    continue
                echo = data.get("echo")
                if echo is not None and str(echo) in self._pending:
                    fut = self._pending.pop(str(echo))
                    if not fut.done():
                        fut.set_result(data)
                elif "post_type" in data:
                    try:
                        self._on_event(data)
                    except Exception:
                        log.exception("event handler failed")
        except websockets.ConnectionClosed:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------ actions

    async def call(self, action: str, params: dict | None = None, timeout: float | None = None) -> Any:
        ws = self._ws
        if ws is None:
            raise LinkDown("not connected")
        timeout = timeout or self.cfg.call_timeout_s
        echo = str(next(self._echo))
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        t0 = time.monotonic()
        logs.trace(log, "action> %s %s echo=%s", action, params, echo)
        try:
            await ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo},
                                     ensure_ascii=False))
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{action} timed out after {timeout}s") from None
        except websockets.ConnectionClosed:
            raise LinkDown("connection closed while sending") from None
        finally:
            self._pending.pop(echo, None)
        ms = (time.monotonic() - t0) * 1000
        retcode = resp.get("retcode", 0)
        status = resp.get("status", "ok")
        logs.trace(log, "action< %s retcode=%s status=%s %.0fms", action, retcode, status, ms)
        if status == "failed" or retcode not in (0, 1, None):
            raise OneBotError(action, int(retcode or -1), str(resp.get("wording") or resp.get("msg") or ""))
        return resp.get("data")

    def call_sync(self, action: str, params: dict | None = None, timeout: float | None = None) -> Any:
        """For Dice/worker threads. Never call from the event-loop thread (it would deadlock)."""
        if self._loop is None:
            raise LinkDown("link not running")
        if threading.get_ident() == self._loop_thread:
            raise RuntimeError("call_sync used on the event-loop thread")
        timeout = timeout or self.cfg.call_timeout_s
        fut = asyncio.run_coroutine_threadsafe(self.call(action, params, timeout), self._loop)
        try:
            return fut.result(timeout + 2)
        except TimeoutError:
            fut.cancel()
            raise

    def call_nowait(self, action: str, params: dict | None, what: str = "", timeout: float | None = None) -> None:
        """Fire-and-forget from any thread; the outcome is logged."""
        if self._loop is None:
            log.error("%s dropped: link not running", what or action)
            return

        async def runner():
            try:
                await self.call(action, params, timeout)
                log.debug("%s ok", what or action)
            except OneBotError as e:
                log.warning("%s failed: %s", what or action, e)
            except Exception as e:
                log.error("%s failed: %s: %s", what or action, type(e).__name__, e)

        def spawn():
            task = self._loop.create_task(runner())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        self._loop.call_soon_threadsafe(spawn)

