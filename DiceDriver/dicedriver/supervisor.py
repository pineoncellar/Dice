"""Supervisor: runs the driver as a child process so that no death goes unrecorded.

The driver cannot always explain its own death: a native fault inside the Dice DLL takes the whole
process down with it, and nothing inside that process gets to speak afterwards. A parent process
cannot be taken down with it, so this one always sees the exit code, always writes it down, always
leaves an incident report, and starts a replacement.

  logs/supervisor.log     one line per start, exit, restart and stall
  logs/incidents/*.txt    one report per abnormal death: exit code plus the tail of every forensic
                          file the driver left behind, plus how to get a native stack next time
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .crashguard import RESTART_EXIT_CODE, SUPERVISED_ENV, decode_exit_code, format_duration

TAIL_LINES = 60


def _stamp() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def _tail(path: Path, lines: int = TAIL_LINES, max_bytes: int = 65536) -> str:
    """The last `lines` lines of a file, without pulling a 100 MB log into memory."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line
            data = f.read()
    except OSError as e:
        return f"(cannot read {path}: {e})\n"
    rows = data.decode("utf-8", "replace").splitlines()
    return ("\n".join(rows[-lines:]) + "\n") if rows else "(empty)\n"


class Supervisor:
    def __init__(self, child_argv: list[str], log_dir: Path, *, restart: bool = True,
                 restart_delay: float = 3.0, max_restarts: int = 5, restart_window_s: float = 600.0,
                 stall_timeout: float = 0.0, alive_file: Path | None = None,
                 tail_files: list[Path] | None = None, cwd: str | None = None,
                 env: dict[str, str] | None = None):
        self.child_argv = child_argv
        self.log_dir = Path(log_dir)
        self.restart = restart
        self.restart_delay = max(0.0, restart_delay)
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.stall_timeout = stall_timeout
        self.alive_file = Path(alive_file) if alive_file else None
        self.tail_files = [Path(p) for p in (tail_files or [])]
        self.incident_dir = self.log_dir / "incidents"
        self.cwd = cwd
        self.env = dict(os.environ if env is None else env)
        self.env[SUPERVISED_ENV] = "1"
        self._log_handle = None
        self._spawn_wall = 0.0
        self._stop = threading.Event()

    # ------------------------------------------------------------------ logging

    def log(self, message: str) -> None:
        """File first, console second: a window that stops draining must not cost us the record."""
        line = f"{_stamp()} {message}\n"
        if self._log_handle is None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self.log_dir / "supervisor.log", "a", encoding="utf-8",
                                    errors="replace", buffering=1)
        try:
            self._log_handle.write(line)
            self._log_handle.flush()
        except Exception:
            pass
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------ child

    def _spawn(self) -> subprocess.Popen:
        self.incident_dir.mkdir(parents=True, exist_ok=True)
        self._spawn_wall = time.time()
        # stdout/stderr are inherited on purpose: the child copies them to logs\stderr.log itself,
        # and a pipe here would let a blocked console freeze the child through a full pipe buffer.
        return subprocess.Popen(self.child_argv, cwd=self.cwd, env=self.env)

    def _stalled(self) -> float:
        """Seconds since the last sign of life, judged from the liveness file the child keeps."""
        if not self.stall_timeout:
            return 0.0
        freshness = self._spawn_wall
        if self.alive_file is not None:
            try:
                freshness = max(freshness, self.alive_file.stat().st_mtime)
            except OSError:
                pass
        age = time.time() - freshness
        return age if age > self.stall_timeout else 0.0

    def _kill(self, child: subprocess.Popen) -> None:
        """Kill the whole tree: `python.exe` may itself be a launcher whose real process is a child."""
        if sys.platform == "win32":
            try:
                done = subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)],
                                      capture_output=True, text=True)
                if done.returncode == 0:
                    return
            except OSError:
                pass
        try:
            child.kill()
        except OSError as e:
            self.log(f"could not kill pid {child.pid}: {e}")

    def _wait(self, child: subprocess.Popen) -> str:
        """Block until the child is gone. Returns 'exit', 'kill' (stall) or 'user' (stop signal)."""
        while True:
            try:
                child.wait(timeout=1.0)
                return "user" if self._stop.is_set() else "exit"
            except subprocess.TimeoutExpired:
                pass
            except KeyboardInterrupt:
                if not self._stop.is_set():
                    self.log("Ctrl+C in this window: letting the driver stop cleanly")
                    self._stop.set()
                continue
            if self._stop.is_set():
                # The driver sits in the same console group, so it received the same signal and
                # is stopping on its own; give it time to flush its state before taking it out.
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.log("the driver did not stop within 30s of the stop signal; killing it")
                    self._kill(child)
                return "user"
            stalled = self._stalled()
            if stalled:
                self.log(f"STALL: no sign of life for {stalled:.0f}s; killing pid {child.pid}")
                self._kill(child)
                return "kill"

    # ------------------------------------------------------------------ incidents

    def _incident(self, pid: int, code: int, uptime: float, detail: str, restarts: int) -> Path:
        when = time.strftime("%Y%m%d-%H%M%S")
        self.incident_dir.mkdir(parents=True, exist_ok=True)
        path = self.incident_dir / f"incident-{when}-pid{pid}.txt"
        parts = [
            "DiceDriver incident report",
            "==========================",
            f"time        : {_stamp()}",
            f"child pid   : {pid}",
            f"exit code   : {code} (0x{code & 0xFFFFFFFF:08X})",
            f"meaning     : {detail}",
            f"uptime      : {format_duration(uptime)}",
            f"command     : {' '.join(self.child_argv)}",
            f"abnormal exits in the last {self.restart_window_s:.0f}s: {restarts}",
            "",
            "What to do with this file:",
            "  1) Read the tails below. crash.log ends without a 'driver exiting' line for a run",
            "     that died hard; the last stacks it holds are the Python side of the fault.",
            "  2) The exit code is the fastest classifier: 0xC0000005 is an access violation",
            "     (usually inside the Dice DLL), 0xC0000409/0xC0000374 a stack/heap corruption.",
            "  3) For a native stack, make Windows keep a dump of the next one and open it in WinDbg:",
            '       reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting\\LocalDumps\\python.exe"'
            ' /v DumpFolder /t REG_EXPAND_SZ /d "C:\\dumps" /f',
            '       reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting\\LocalDumps\\python.exe"'
            " /v DumpType /t REG_DWORD /d 2 /f",
            "     and check the event log for the same crash:",
            "       Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000} -MaxEvents 5 | Format-List",
            "",
        ]
        for f in self.tail_files:
            parts += [f"--- tail of {f} ---", _tail(f)]
        try:
            path.write_text("\n".join(parts), encoding="utf-8")
        except OSError as e:
            self.log(f"could not write the incident report {path}: {e}")
        return path

    # ------------------------------------------------------------------ run

    def run(self) -> int:
        self.log(f"supervisor started (pid {os.getpid()}) for: {' '.join(self.child_argv)}")
        self.log(f"logs: {self.log_dir}" + (f", liveness: {self.alive_file}" if self.alive_file else ""))
        self._arm_signals()
        if self.restart:
            self.log(f"restart policy: delay {self.restart_delay:.0f}s, at most {self.max_restarts} "
                     f"abnormal exits per {self.restart_window_s:.0f}s")
        else:
            self.log("restart policy: off")
        if self.stall_timeout:
            self.log(f"stall watchdog: kill after {self.stall_timeout:.0f}s without a sign of life")
        else:
            self.log("stall watchdog: off")

        abnormal: list[float] = []
        while True:
            child = self._spawn()
            self.log(f"child started (pid {child.pid})")
            started = time.monotonic()
            how = self._wait(child)
            uptime = time.monotonic() - started
            code = child.returncode if child.returncode is not None else 1
            detail = ("killed by the supervisor: the driver stopped showing signs of life"
                      if how == "kill" else decode_exit_code(code))
            self.log(f"child {child.pid} exited after {format_duration(uptime)}: "
                     f"code {code} - {detail}")
            if how == "user":
                self.log("stopped from this window: not restarting")
                return 0 if code == 0 else code
            if code == 0:
                self.log("clean exit: not restarting (a supervised Dice Reload asks for 75 instead)")
                return 0
            if code == 2:
                self.log("code 2 = configuration or DLL problem; fix that first, not restarting")
                return 2

            now = time.monotonic()
            abnormal = [t for t in abnormal if now - t < self.restart_window_s]
            abnormal.append(now)
            path = self._incident(child.pid, code, uptime, detail, len(abnormal))
            self.log("------------------------------------------------------------")
            self.log(f"ABNORMAL EXIT: {detail}")
            self.log(f"incident report: {path}")
            self.log("------------------------------------------------------------")
            if not self.restart:
                self.log("restart is off; stopping here")
                return code
            if self.max_restarts and len(abnormal) > self.max_restarts:
                self.log(f"CRASH LOOP: {len(abnormal)} abnormal exits within "
                         f"{self.restart_window_s:.0f}s - giving up. Read {self.incident_dir} first.")
                return code
            if code != RESTART_EXIT_CODE and self.restart_delay:
                self.log(f"restarting in {self.restart_delay:.0f}s (abnormal exit #{len(abnormal)})")
                if self._stop.wait(self.restart_delay):
                    self.log("stopped from this window during the restart delay")
                    return code
            else:
                self.log(f"restarting now (abnormal exit #{len(abnormal)})")

    def _arm_signals(self) -> None:
        """Ctrl+C and Ctrl+Break have to reach us as "stop", not as an abrupt death of the watcher."""
        def on_signal(signum, _frame):
            if not self._stop.is_set():
                self.log(f"stop signal {signum}: letting the driver stop cleanly (no restart)")
            self._stop.set()

        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass

    def close(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None


def main(cfg, config_path: Path, *, restart: bool = True, restart_delay: float = 3.0,
         max_restarts: int = 5, restart_window_s: float = 600.0,
         stall_timeout: float | None = None, child_extra: list[str] | None = None) -> int:
    """Run the driver under supervision; returns the child's last exit code."""
    log_dir = cfg.resolve(cfg.log.file).parent
    alive = cfg.resolve(cfg.log.alive_file)
    if stall_timeout is None:
        # The supervisor can only see a stall through the liveness file the driver keeps.
        if cfg.log.stall_s:
            stall_timeout = cfg.log.stall_s
        elif cfg.log.liveness_s:
            stall_timeout = cfg.log.liveness_s * 3
        else:
            stall_timeout = 0.0
    tails = [cfg.resolve(cfg.log.crash_file), cfg.resolve(cfg.log.journal_file),
             cfg.resolve(cfg.log.stderr_file), cfg.resolve(cfg.log.file), alive]
    child = [sys.executable, "-m", "dicedriver", "--config", str(config_path)]
    child += child_extra or []
    log_dir.mkdir(parents=True, exist_ok=True)
    sup = Supervisor(child, log_dir, restart=restart, restart_delay=restart_delay,
                     max_restarts=max_restarts, restart_window_s=restart_window_s,
                     stall_timeout=stall_timeout or 0.0, alive_file=alive,
                     tail_files=[p for p in tails if p], cwd=os.getcwd())
    try:
        return sup.run()
    finally:
        sup.close()
