"""Logging: rotating main log (custom TRACE level), separate Dice log, non-blocking writers.

Three tiers, because "the process died and left nothing behind" is the worst failure mode:
  * journal - written in the calling thread and flushed per record, so a record is durable
              before the process can disappear. Only >= info lands here (a few lines an hour).
  * file    - the verbose rotating log, drained by its own listener thread.
  * console - stderr, drained by a *separate* listener thread, so a blocked console
              (QuickEdit selection, Ctrl+S, a paused pipe) can never stall the file tier.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import time
from pathlib import Path

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_bot_id = "-"
_listeners: list[logging.handlers.QueueListener] = []
_handlers: list[logging.Handler] = []  # every handler we own, so shutdown() can flush them itself
_queues: dict[str, queue.SimpleQueue] = {}  # per-tier backlog, surfaced in the liveness line
_tees: list["_Tee"] = []


def set_bot_id(qq: int | str) -> None:
    global _bot_id
    _bot_id = str(qq)


class _BotFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.bot = _bot_id
        return True


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.levelname = record.levelname.lower()
        return super().format(record)


_FMT = "%(asctime)s.%(msecs)03d [%(levelname)s] [bot=%(bot)s] [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def trace(logger: logging.Logger, msg: str, *args) -> None:
    if logger.isEnabledFor(TRACE):
        logger.log(TRACE, msg, *args)


def _rotating(path: Path, max_mb: int, files: int) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    return logging.handlers.RotatingFileHandler(
        path, maxBytes=max(1, max_mb) * 1024 * 1024, backupCount=max(1, files), encoding="utf-8"
    )


class _Tee:
    """Copy a stream into a file *before* writing it to the real stream.

    The file comes first on purpose: whatever then blocks the console (a selection made in the
    window, Ctrl+S, a pipe nobody reads) can no longer take the text down with it.
    """

    def __init__(self, path: Path, stream):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.stream = stream
        self._file = open(path, "a", encoding="utf-8", errors="replace", buffering=1, newline="")

    def write(self, text: str) -> int:
        try:
            self._file.write(text)
        except Exception:  # a failing disk must not turn into a crash inside the logging path
            pass
        try:
            return self.stream.write(text)
        except Exception:
            return len(text)

    def flush(self) -> None:
        for part in (self._file, self.stream):
            try:
                part.flush()
            except Exception:
                pass

    def close(self) -> None:
        self.flush()
        try:
            self._file.close()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    def fileno(self):
        return self.stream.fileno()

    @property
    def encoding(self) -> str:
        return "utf-8"


def _install_tee(cfg) -> None:
    """Copy sys.stdout/sys.stderr into one file, so closing the window cannot erase the text."""
    target = cfg.log.stderr_file
    if not target:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, _Tee):
            continue
        try:
            tee = _Tee(cfg.resolve(target), stream)
        except OSError:
            continue  # unreadable log directory: keep the plain stream rather than fail to start
        setattr(sys, name, tee)
        _tees.append(tee)


def _async(tier: str, handlers: list[logging.Handler]) -> logging.handlers.QueueHandler:
    """One listener thread per tier."""
    q: queue.SimpleQueue = queue.SimpleQueue()
    _queues[tier] = q
    listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    listener.start()
    # QueueListener runs the drain on its own Thread object: name it, so a thread dump reads
    # as "the file drain is stuck" instead of "Thread-6 (_monitor)".
    thread = getattr(listener, "_thread", None)
    if thread is not None:
        thread.name = f"dd-log-{tier}"
    _listeners.append(listener)
    qh = logging.handlers.QueueHandler(q)
    qh.addFilter(_BotFilter())
    return qh


def queue_depth() -> dict[str, int]:
    """Backlog per tier: a growing file tier means a slow disk, a growing console tier a stuck window."""
    return {name: q.qsize() for name, q in _queues.items()}


def setup(cfg) -> None:
    """Configure the `dd` logger tree (the driver's own log)."""
    shutdown()
    level = _LEVELS[cfg.log.level.lower()]
    _install_tee(cfg)
    fmt = _Formatter(_FMT, _DATEFMT)

    # Durable tier: no queue in front of it, flushed per record.
    journal = _rotating(cfg.resolve(cfg.log.journal_file), cfg.log.max_size_mb, cfg.log.files)
    journal.setLevel(max(level, logging.INFO))
    # Verbose tier: the rotating log everyone reads, plus stderr when a console is wanted.
    main = _rotating(cfg.resolve(cfg.log.file), cfg.log.max_size_mb, cfg.log.files)

    for h in (journal, main):
        h.setFormatter(fmt)
        h.addFilter(_BotFilter())
        _handlers.append(h)

    root = logging.getLogger("dd")
    root.handlers.clear()
    root.setLevel(level)
    root.propagate = False
    root.addHandler(journal)
    root.addHandler(_async("file", [main]))
    if cfg.log.console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        console.addFilter(_BotFilter())
        _handlers.append(console)
        root.addHandler(_async("console", [console]))


def bind_dice_log(cfg, qq: int | str) -> None:
    """Once the bot QQ is known, Dice's DebugLog text goes to its own file, apart from the main log."""
    set_bot_id(qq)
    dice = logging.getLogger("dice")
    dice.setLevel(logging.DEBUG)
    dice.propagate = False
    for h in list(dice.handlers):
        dice.removeHandler(h)
    handler = _rotating(cfg.resolve(cfg.log.dice_log_file.replace("{qq}", str(qq))), cfg.log.max_size_mb, cfg.log.files)
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", _DATEFMT))
    _handlers.append(handler)
    dice.addHandler(_async("dice", [handler]))


def shutdown(timeout: float = 5.0) -> None:
    """Drain the listeners, then flush and close every handler ourselves.

    The second half matters: on the way out we may be holding a listener thread that is stuck on
    a blocked console, and the tail of the log is exactly what someone will come looking for.
    """
    listeners, _listeners[:] = list(_listeners), []
    for lst in listeners:
        try:
            lst.enqueue_sentinel()
        except Exception:
            pass
    deadline = time.monotonic() + timeout
    for lst in listeners:
        thread = getattr(lst, "_thread", None)
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
    for name in ("dd", "dice"):
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            logger.removeHandler(h)
    for h in _handlers:
        try:
            h.flush()
            h.close()
        except Exception:
            pass
    _handlers.clear()
    _queues.clear()
    for tee in _tees:
        if getattr(sys, "stdout", None) is tee:
            sys.stdout = tee.stream
        if getattr(sys, "stderr", None) is tee:
            sys.stderr = tee.stream
        tee.close()
    _tees.clear()
