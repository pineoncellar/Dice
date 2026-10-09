"""Orchestration: wires config, OneBot link, state, host API, Dice DLL and events together."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import logs
from .config import Config
from .events import EventRouter
from .hostapi import HostApi
from .native import DiceNative
from .notify import Heartbeat, Notifier
from .onebot import OneBotLink
from .state import State

log = logging.getLogger("dd.app")

PACKAGE_PARENT = Path(__file__).resolve().parent.parent


class FatalError(RuntimeError):
    pass


class App:
    def __init__(self, cfg: Config, config_path: Path):
        self.cfg = cfg
        self.config_path = config_path
        self._bot_qq = cfg.driver.bot_qq
        self._ready = False
        self._started = False
        self._stopping = threading.Event()
        self._restarting = threading.Lock()
        self._exit_code = 0
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self.executor = ThreadPoolExecutor(max_workers=cfg.driver.pool_size, thread_name_prefix="dd-worker")
        self.link = OneBotLink(cfg.onebot, self._on_event, self._on_connected, self._on_disconnected)
        self.state = State(self.link, cfg.cache, cfg.resolve(cfg.driver.state_dir))
        self.notifier = Notifier(cfg.notify, self.link)
        self.heartbeat = Heartbeat(cfg.heartbeat)
        self.api = HostApi(cfg, self.link, self.state, self.notifier, self.heartbeat, self)
        self.native = DiceNative(cfg.resolve(cfg.driver.shim_dll), cfg.resolve(cfg.driver.dice_dll), self.api)
        self.router = EventRouter(cfg, self.state, self.native, lambda: self.self_id, self.executor,
                                  lambda: self._ready)

    # ------------------------------------------------------------------ lifecycle interface used by HostApi

    @property
    def self_id(self) -> int:
        return self._bot_qq or self.link.self_id

    @property
    def root_dir(self) -> Path:
        return self.cfg.resolve(self.cfg.driver.root_dir).resolve()

    def restart(self, reason: str) -> None:
        """Replace this process with a fresh one (Reload and Remake both land here)."""
        if not self._restarting.acquire(blocking=False):
            log.warning("restart already in progress, ignoring %s", reason)
            return
        threading.Thread(target=self._restart_worker, args=(reason,), name="dd-restart", daemon=True).start()

    def _restart_worker(self, reason: str) -> None:
        time.sleep(2.0)  # let Dice's own confirmation message leave first
        if reason == "reload":  # Remake: Dice already stopped itself and backed up its data
            self._stop_dice(timeout=15)
        log.warning("starting replacement process (%s)", reason)
        cmd = [sys.executable, "-m", "dicedriver", "--config", str(self.config_path),
               "--wait-pid", str(os.getpid())]
        try:
            subprocess.Popen(cmd, cwd=str(PACKAGE_PARENT))
        except OSError:
            log.exception("failed to start the replacement process; staying alive")
            self._restarting.release()
            return
        self._finish(0)

    def exit(self) -> None:
        def worker():
            time.sleep(0.5)
            self._finish(0)
        threading.Thread(target=worker, name="dd-exit", daemon=True).start()

    def _stop_dice(self, timeout: float) -> None:
        def worker():
            try:
                self.native.disable()
                self.native.exit()
            except Exception:
                log.exception("Dice shutdown failed")
        t = threading.Thread(target=worker, name="dd-dice-stop", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            log.error("Dice did not stop within %ss; continuing anyway", timeout)

    def _finish(self, code: int) -> None:
        """Flush everything and terminate the process; Dice's detached threads would block a normal exit."""
        log.info("driver exiting (code %d)", code)
        try:
            self.state.flush(force=True)
            self.notifier.stop()
            self.heartbeat.stop()
        finally:
            logs.shutdown()
            os._exit(code)

    # ------------------------------------------------------------------ OneBot link callbacks

    def _on_event(self, ev: dict) -> None:
        self.router.on_event(ev)

    async def _on_connected(self, self_id: int) -> None:
        if self._bot_qq and self_id != self._bot_qq:
            raise FatalError(f"connected account {self_id} differs from the running bot {self._bot_qq}")
        loop = asyncio.get_running_loop()
        if not self._started:
            self._bot_qq = self_id
            try:
                await asyncio.wait_for(loop.run_in_executor(self.executor, self._startup, self_id),
                                       self.cfg.driver.startup_timeout_s)
            except Exception as e:
                log.exception("startup failed")
                self._fatal(1)
                raise FatalError(str(e)) from e
        else:
            log.info("reconnected as %d, refreshing caches", self_id)
            await loop.run_in_executor(self.executor, self._refresh_after_reconnect)

    def _on_disconnected(self) -> None:
        log.warning("OneBot connection lost; Dice keeps running, outgoing messages fail until reconnect")

    def _fatal(self, code: int) -> None:
        self._exit_code = code
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)

    # ------------------------------------------------------------------ startup (worker thread)

    def _startup(self, self_id: int) -> None:
        logs.bind_dice_log(self.cfg, self_id)
        log.info("OneBot account %d connected; starting Dice", self_id)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.state.dir.mkdir(parents=True, exist_ok=True)
        self.native.load()
        if not self.state.refresh_groups():
            log.error("group list unavailable at startup; Dice will see no groups until the next refresh")
        self.state.friends()
        self.state.nick(self_id, self_id)
        t0 = time.monotonic()
        self.native.start(self_id)
        log.info("Dice started in %.0f ms", (time.monotonic() - t0) * 1000)
        self._started = True
        self._ready = True
        threading.Thread(target=self._maintenance, name="dd-maintenance", daemon=True).start()

    def _refresh_after_reconnect(self) -> None:
        self.state.refresh_groups()
        self.state.invalidate_friends()
        self.state.friends()

    def _maintenance(self) -> None:
        while not self._stopping.wait(1.0):
            if not self.link.connected:
                continue
            try:
                self.state.tick()
            except Exception:
                log.exception("maintenance tick failed")

    # ------------------------------------------------------------------ run

    def preflight(self) -> None:
        for label, p in (("shim DLL", self.cfg.resolve(self.cfg.driver.shim_dll)),
                         ("Dice DLL", self.cfg.resolve(self.cfg.driver.dice_dll))):
            if not p.is_file():
                raise FatalError(f"{label} not found: {p}")

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        def request_stop(*_):
            log.info("stop requested")
            self._loop.call_soon_threadsafe(self._stop.set)

        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, request_stop)
                except (ValueError, OSError):
                    pass

        link_task = asyncio.create_task(self.link.run())
        stop_task = asyncio.create_task(self._stop.wait())
        done, _ = await asyncio.wait({link_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if link_task in done and not self._stop.is_set():
            exc = link_task.exception()
            log.error("OneBot link ended: %s", exc or "unexpectedly")
            self._exit_code = self._exit_code or 1
        await self.link.close()
        link_task.cancel()
        self._stopping.set()
        if self._ready:
            log.info("stopping Dice")
            await asyncio.get_running_loop().run_in_executor(None, self._stop_dice, 20)
        return self._exit_code

    def finish(self, code: int) -> None:
        self._finish(code)
