"""Entry point: `python -m dicedriver --config dicedriver.toml`."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import sys
from pathlib import Path

from . import crashguard, logs
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
    ap.add_argument("--supervise", action="store_true",
                    help="run the driver as a child of a supervisor that records every exit and restarts it")
    ap.add_argument("--no-restart", action="store_true", help="supervisor: record deaths, do not restart")
    ap.add_argument("--restart-delay", type=float, default=3.0, help="supervisor: seconds before a restart")
    ap.add_argument("--max-restarts", type=int, default=5,
                    help="supervisor: abnormal exits tolerated inside the restart window (0 = unlimited)")
    ap.add_argument("--restart-window", type=float, default=600.0, help="supervisor: restart window in seconds")
    ap.add_argument("--stall-timeout", type=float, default=None,
                    help="supervisor: kill the driver after this many seconds without a sign of life (0 = off, "
                         "default: [log] stall_s)")
    args = ap.parse_args(argv)

    if args.wait_pid:
        _wait_for_pid(args.wait_pid)

    config_path = Path(args.config).resolve()
    try:
        cfg = load(config_path)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.supervise and not args.check:
        from . import supervisor
        return supervisor.main(cfg, config_path, restart=not args.no_restart,
                               restart_delay=args.restart_delay, max_restarts=args.max_restarts,
                               restart_window_s=args.restart_window, stall_timeout=args.stall_timeout)

    logs.setup(cfg)
    crashguard.install(cfg)
    app = App(cfg, config_path)
    try:
        app.preflight()
    except FatalError as e:
        logging.getLogger("dd").error("%s", e)
        crashguard.note_shutdown(2, "preflight failed")
        crashguard.stop()
        logs.shutdown()
        return 2
    if args.check:
        print(f"OK: config {config_path}\n    mode={cfg.onebot.mode} root={app.root_dir}")
        crashguard.stop()
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
    except BaseException:
        # An exception on the way out still has to leave a durable trace: the traceback goes into
        # the journal (written synchronously) and the crash log before it reaches the console.
        log.exception("driver crashed; exiting with code 1")
        crashguard.note_shutdown(1, "uncaught exception")
        try:
            app.state.flush(force=True)
            app.notifier.stop()
            app.heartbeat.stop()
        except Exception:
            log.exception("cleanup after the crash failed")
        logs.shutdown()
        raise
    app.finish(code)
    return code  # unreachable: finish() ends the process after flushing


if __name__ == "__main__":
    sys.exit(main())
