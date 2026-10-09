"""Logging: rotating main log (custom TRACE level), separate Dice log, non-blocking writers."""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
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


def _async(handlers: list[logging.Handler]) -> logging.handlers.QueueHandler:
    q: queue.SimpleQueue = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    listener.start()
    _listeners.append(listener)
    return logging.handlers.QueueHandler(q)


def setup(cfg) -> None:
    """Configure the `dd` logger tree (the driver's own log)."""
    level = _LEVELS[cfg.log.level.lower()]
    handlers: list[logging.Handler] = [_rotating(cfg.resolve(cfg.log.file), cfg.log.max_size_mb, cfg.log.files)]
    if cfg.log.console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(_Formatter(_FMT, _DATEFMT))

    root = logging.getLogger("dd")
    root.handlers.clear()
    root.setLevel(level)
    root.propagate = False
    qh = _async(handlers)
    qh.addFilter(_BotFilter())
    root.addHandler(qh)


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
    dice.addHandler(_async([handler]))


def shutdown() -> None:
    for lst in _listeners:
        lst.stop()
    _listeners.clear()
