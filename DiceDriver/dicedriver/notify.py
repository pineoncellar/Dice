"""DebugMsg delivery (batched, rate limited) and DiceHeartbeat POSTing (queued, short timeout)."""

from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("dd.notify")


class Notifier:
    def __init__(self, cfg, bot):
        self.cfg = cfg
        self.bot = bot
        self._q: queue.Queue[str | None] = queue.Queue()
        self._sent_at: list[float] = []
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, name="dd-notify", daemon=True)
        self._thread.start()

    def push(self, text: str) -> None:
        if self.cfg.mode == "off":
            return
        log.info("DebugMsg: %s", text)
        if self.cfg.mode != "console":
            self._q.put(text)

    def stop(self) -> None:
        self._q.put(None)

    def _run(self) -> None:
        window = max(self.cfg.batch_window_ms, 0) / 1000
        while True:
            first = self._q.get()
            if first is None:
                return
            batch = [first]
            deadline = time.monotonic() + window
            stop = False
            while (left := deadline - time.monotonic()) > 0:
                try:
                    item = self._q.get(timeout=left)
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    break
                batch.append(item)
            self._deliver("\n".join(batch))
            if stop:
                return

    def _deliver(self, text: str) -> None:
        now = time.monotonic()
        self._sent_at = [t for t in self._sent_at if now - t < 60]
        if len(self._sent_at) >= self.cfg.rate_limit:
            self._dropped += 1
            log.warning("notification rate limit (%d/min) hit, dropped (total dropped %d)",
                        self.cfg.rate_limit, self._dropped)
            return
        if self._dropped:
            text += f"\n(另有 {self._dropped} 批通知因限流被丢弃)"
            self._dropped = 0
        self._sent_at.append(now)
        message = [{"type": "text", "data": {"text": text}}]
        for target in self.cfg.targets:
            if self.cfg.mode == "private":
                self.bot.call_nowait("send_private_msg", {"user_id": target, "message": message},
                                     f"notify private {target}")
            else:
                self.bot.call_nowait("send_group_msg", {"group_id": target, "message": message},
                                     f"notify group {target}")


class Heartbeat:
    def __init__(self, cfg):
        self.cfg = cfg
        self._q: queue.Queue[str | None] = queue.Queue(maxsize=100)
        self._thread = None
        if cfg.enabled:
            self._thread = threading.Thread(target=self._run, name="dd-heartbeat", daemon=True)
            self._thread.start()

    def push(self, payload: str) -> None:
        if not self.cfg.enabled:
            log.debug("heartbeat ignored (disabled): %s", payload)
            return
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            log.warning("heartbeat queue full, dropped")

    def stop(self) -> None:
        if self._thread:
            self._q.put(None)

    def _run(self) -> None:
        while True:
            payload = self._q.get()
            if payload is None:
                return
            req = urllib.request.Request(self.cfg.url, data=payload.encode("utf-8"), method="POST")
            req.add_header("Content-Type", self.cfg.content_type)
            for k, v in self.cfg.headers.items():
                req.add_header(k, str(v))
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_ms / 1000) as resp:
                    log.debug("heartbeat posted: status=%s %.0fms", resp.status, (time.monotonic() - t0) * 1000)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                log.warning("heartbeat POST to %s failed: %s", self.cfg.url, e)
            except Exception:
                log.exception("heartbeat POST crashed")
