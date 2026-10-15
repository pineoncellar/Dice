"""Crash forensics: make a dying process leave evidence behind.

Nothing here can save the process; the point is that whatever kills it - an access violation
inside the Dice DLL, an uncaught exception on a worker thread, a wedged thread pool, a console
that stopped draining - leaves a file behind that says so.

  * faulthandler on a dedicated file: native faults dump every thread's Python stack instead of
    vanishing with the process.
  * exception hooks: anything Python lets escape ends up in the same file.
  * a watchdog thread: keeps `logs/alive.json` fresh and dumps every stack once no source has
    ticked for `log.stall_s`, which is the "window froze and printed nothing" case.
"""

from __future__ import annotations

import collections
import faulthandler
import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import logs

log = logging.getLogger("dd.crash")

# The driver exits with this code when it wants its supervisor to start a fresh process
# (Dice's Reload/Remake). The supervisor is then the only thing that spawns processes.
RESTART_EXIT_CODE = 75

# Set by the supervisor in the child's environment; the driver reads it to hand the
# process lifecycle over instead of forking a replacement nobody would be watching.
SUPERVISED_ENV = "DICEDRIVER_SUPERVISED"

# Windows status codes that reach us as a process exit code. A silent death is usually one of these.
_FATAL_CODES = {
    0xC0000005: "ACCESS_VIOLATION (dereferenced a bad pointer)",
    0xC0000006: "IN_PAGE_ERROR (a mapped file/DLL went away)",
    0xC0000008: "INVALID_HANDLE",
    0xC000000D: "INVALID_PARAMETER",
    0xC0000017: "NO_MEMORY (commit limit reached)",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000025: "NONCONTINUABLE_EXCEPTION",
    0xC000008C: "ARRAY_BOUNDS_EXCEEDED",
    0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC0000095: "INTEGER_OVERFLOW",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000135: "DLL_NOT_FOUND",
    0xC000013A: "CONTROL_C_EXIT (Ctrl+C or the console window was closed)",
    0xC0000142: "DLL_INIT_FAILED",
    0xC0000374: "HEAP_CORRUPTION",
    0xC0000409: "STACK_BUFFER_OVERRUN (fast-fail)",
    0xC0000602: "FAIL_FAST_EXCEPTION",
    0x40000015: "FATAL_APP_EXIT (abort)",
    0xC06D007E: "MODULE_NOT_FOUND (delay-loaded DLL missing)",
    0xC06D007F: "PROCEDURE_NOT_FOUND (delay-loaded import missing)",
}


def decode_exit_code(code: int | None) -> str:
    """Turn a Popen returncode into something a human can act on."""
    if code is None:
        return "still running"
    if code == 0:
        return "clean exit"
    if code == RESTART_EXIT_CODE:
        return "supervisor restart requested (Dice Reload/Remake)"
    unsigned = code & 0xFFFFFFFF
    known = _FATAL_CODES.get(unsigned)
    if known:
        return f"{known}, 0x{unsigned:08X}"
    if unsigned >= 0x80000000:
        return f"fatal Windows status 0x{unsigned:08X} (the process died without unwinding)"
    return f"exit code {code}"


def _stamp() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return (f"{days}d" if days else "") + f"{hours}h{rest // 60:02d}m{rest % 60:02d}s"


class _State:
    def __init__(self, cfg):
        self.crash_path: Path = cfg.resolve(cfg.log.crash_file)
        self.alive_path: Path = cfg.resolve(cfg.log.alive_file)
        self.liveness_s: float = cfg.log.liveness_s
        self.stall_s: float = cfg.log.stall_s
        # The supervisor watches this file, so it has to exist whenever either knob is on.
        self.alive_interval: float = cfg.log.liveness_s or (cfg.log.stall_s / 3 if cfg.log.stall_s else 0.0)
        self.started = time.monotonic()
        self.last_tick = time.monotonic()
        self.ticks: dict[str, float] = {}
        self.last_event: tuple[float, str] = (time.monotonic(), "none yet")
        self.ops: collections.deque[str] = collections.deque(maxlen=32)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_stall_report = 0.0
        self.crash = None
        try:
            self.crash_path.parent.mkdir(parents=True, exist_ok=True)
            # Unbuffered: whatever reaches this file is in the OS before the process can die.
            self.crash = open(self.crash_path, "ab", buffering=0)
        except OSError as e:
            log.error("cannot open the crash log %s (%s); falling back to stderr", self.crash_path, e)

    # ------------------------------------------------------------------ writing

    def record(self, text: str) -> None:
        """Append raw text to the crash log. Never raises: this runs while things are failing."""
        if self.crash is None:
            return
        with self._lock:
            try:
                self.crash.write(text.encode("utf-8", "replace"))
            except Exception:
                pass

    def alive(self, now: float) -> None:
        data = {
            "pid": os.getpid(),
            "time": _stamp(),
            "uptime_s": round(now - self.started, 1),
            "tick_age_s": {name: round(now - at, 1) for name, at in self.ticks.items()},
            "last_event": {"age_s": round(now - self.last_event[0], 1), "what": self.last_event[1]},
            "log_queue": logs.queue_depth(),
            "threads": sorted(t.name for t in threading.enumerate()),
            "recent_ops": list(self.ops),
        }
        tmp = self.alive_path.with_name(self.alive_path.name + ".tmp")
        try:
            self.alive_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.alive_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ watchdog

    def watch(self) -> None:
        step = 1.0 if self.stall_s else max(1.0, self.alive_interval)
        next_alive = 0.0
        while not self._stop.wait(step):
            now = time.monotonic()
            if self.alive_interval and now >= next_alive:
                self.alive(now)
                next_alive = now + self.alive_interval
            if self.stall_s and now - self.last_tick >= self.stall_s and now - self._last_stall_report >= self.stall_s:
                self._last_stall_report = now
                self.dump_stall(now)

    def dump_stall(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        idle = now - self.last_tick
        ages = ", ".join(f"{name}={now - at:.0f}s" for name, at in sorted(self.ticks.items())) or "none"
        head = (f"\n===== {_stamp()} STALL: nothing ticked for {idle:.0f}s (threshold {self.stall_s:.0f}s), "
                f"pid {os.getpid()}, up {format_duration(now - self.started)} =====\n"
                f"ticks: {ages}\nlast event: {self.last_event[1]} ({now - self.last_event[0]:.0f}s ago)\n"
                f"log queue: {logs.queue_depth()}\nthreads: {', '.join(sorted(t.name for t in threading.enumerate()))}\n"
                f"recent ops: {' | '.join(self.ops) or 'none'}\n"
                f"--- python stacks of every thread (native stacks need a WER dump, see the incident report) ---\n")
        self.record(head)
        try:
            faulthandler.dump_traceback(file=self.crash if self.crash is not None else sys.stderr,
                                        all_threads=True)
        except Exception:
            pass
        self.record("--- end of stacks ---\n")
        try:
            log.critical("STALL: nothing ticked for %.0fs; all thread stacks dumped to %s",
                         idle, self.crash_path)
        except Exception:
            pass
        self.alive(now)


_state: _State | None = None


def state() -> _State | None:
    return _state


# ---------------------------------------------------------------------- hooks


def _emit(kind: str, text: str, where: str = "") -> None:
    head = f"\n===== {_stamp()} {kind}{where}, pid {os.getpid()} =====\n"
    if _state is not None:
        _state.record(head + text)
    summary = text.strip().splitlines()[-1] if text.strip() else kind
    try:
        log.critical("%s%s: %s (full traceback in %s)", kind, where, summary, _state.crash_path if _state else "?")
    except Exception:
        pass
    try:
        sys.stderr.write(head + text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _excepthook(exc_type, exc, tb) -> None:
    try:
        _emit("uncaught exception", "".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:
        pass


def _thread_excepthook(args) -> None:
    try:
        name = args.thread.name if args.thread is not None else "main"
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        _emit("uncaught exception", text, f" in thread {name}")
    except Exception:
        pass


def _unraisablehook(args) -> None:
    try:
        where = f" while ignoring {args.object!r:.120}" if args.object is not None else ""
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        _emit("unraisable exception", text, where)
    except Exception:
        pass


# ---------------------------------------------------------------------- setup


def install(cfg) -> None:
    """Arm faulthandler, the exception hooks and the watchdog. Safe to call more than once."""
    global _state
    stop()
    st = _State(cfg)
    _state = st
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisablehook
    try:
        faulthandler.enable(file=st.crash if st.crash is not None else sys.stderr, all_threads=True)
    except Exception:
        log.exception("faulthandler.enable failed; native faults will leave no stack")
    st.record(f"{_stamp()} ===== driver started, pid {os.getpid()} =====\n")
    if st.alive_interval or st.stall_s:
        threading.Thread(target=st.watch, name="dd-watchdog", daemon=True).start()
    log.info("crash forensics armed: crash=%s alive=%s every %ss, stall=%ss",
             st.crash_path, st.alive_path, st.alive_interval, st.stall_s)


def tick(source: str) -> None:
    """A sign of life from one of the loops; a long silence here is what the watchdog reports."""
    st = _state
    if st is not None:
        now = time.monotonic()
        st.last_tick = now
        st.ticks[source] = now


def note_event(describe: str) -> None:
    st = _state
    if st is not None:
        st.last_event = (time.monotonic(), describe)


def breadcrumb(text: str) -> None:
    """Remember the last few things being done, so a native fault still has a story."""
    st = _state
    if st is not None:
        st.ops.append(text)


def note_shutdown(code: int, reason: str = "") -> None:
    st = _state
    if st is not None:
        st.record(f"{_stamp()} ===== driver exiting: {decode_exit_code(code)}"
                  f"{' (' + reason + ')' if reason else ''} =====\n")


def stop() -> None:
    global _state
    st, _state = _state, None
    if st is None:
        return
    st._stop.set()
    if st.crash is not None:
        try:
            st.crash.flush()
            st.crash.close()
        except Exception:
            pass
    try:
        faulthandler.disable()
    except Exception:
        pass
