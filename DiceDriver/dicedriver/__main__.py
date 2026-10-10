"""Entry point: `python -m dicedriver --config dicedriver.toml`."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import sys
from pathlib import Path

from . import logs
from .app import App, FatalError
from .config import ConfigError, load

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT_MS = 60_000


def _wait_for_pid(pid: int) -> None:
    """A restarted driver waits for its predecessor so the reverse-WS port and log files are free."""
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        ctypes.windll.kernel32.WaitForSingleObject(handle, WAIT_TIMEOUT_MS)
        ctypes.windll.kernel32.CloseHandle(handle)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dicedriver", description="OneBot 11 host for the Dice! plugin DLL")
    ap.add_argument("--config", default="dicedriver.toml", help="path to dicedriver.toml")
    ap.add_argument("--check", action="store_true", help="validate the config and files, then exit")
    ap.add_argument("--wait-pid", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.wait_pid:
        _wait_for_pid(args.wait_pid)

    config_path = Path(args.config).resolve()
    try:
        cfg = load(config_path)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    logs.setup(cfg)
    app = App(cfg, config_path)
    try:
        app.preflight()
    except FatalError as e:
        logging.getLogger("dd").error("%s", e)
        logs.shutdown()
        return 2
    if args.check:
        print(f"OK: config {config_path}\n    mode={cfg.onebot.mode} root={app.root_dir}")
        logs.shutdown()
        return 0

    log = logging.getLogger("dd.main")
    log.info("DiceDriver starting (config %s, mode %s)", config_path, cfg.onebot.mode)
    if cfg.bridge.enabled:
        log.info("relay bridge enabled on ws://%s:%d%s", cfg.bridge.host, cfg.bridge.port, cfg.bridge.path)
    try:
        code = asyncio.run(app.run())
    except KeyboardInterrupt:
        code = 0
    app.finish(code)
    return code  # unreachable: finish() ends the process after flushing


if __name__ == "__main__":
    sys.exit(main())
