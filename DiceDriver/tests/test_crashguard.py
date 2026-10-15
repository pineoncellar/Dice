"""Crash forensics: every way this process can die has to leave a file behind."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from dicedriver import config, crashguard, logs

ROOT = Path(__file__).resolve().parent.parent  # <workspace>\DiceDriver


def _cfg(tmp_path, **log):
    base = {
        "file": str(tmp_path / "main.log"),
        "journal_file": str(tmp_path / "journal.log"),
        "crash_file": str(tmp_path / "crash.log"),
        "stderr_file": str(tmp_path / "stderr.log"),
        "alive_file": str(tmp_path / "alive.json"),
        "console": False,
    }
    base.update(log)
    return config.from_dict({"log": base}, tmp_path)


@pytest.fixture
def cfg(tmp_path):
    c = _cfg(tmp_path)
    logs.setup(c)
    crashguard.install(c)
    yield c
    crashguard.stop()
    logs.shutdown()


def _crash_text(tmp_path: Path) -> str:
    return (tmp_path / "crash.log").read_text(encoding="utf-8")


def test_uncaught_thread_exception_is_recorded(cfg, tmp_path):
    def boom():
        raise ValueError("tornado")

    t = threading.Thread(target=boom, name="dd-test-worker")
    t.start()
    t.join()

    text = _crash_text(tmp_path)
    assert "uncaught exception in thread dd-test-worker" in text
    assert "ValueError: tornado" in text
    # ... and the same summary is in the synchronous journal, so it survives a hard exit too
    assert "ValueError: tornado" in (tmp_path / "journal.log").read_text(encoding="utf-8")


def test_unhandled_main_exception_is_recorded(cfg, tmp_path):
    try:
        raise KeyError("main thread")
    except KeyError:
        sys.excepthook(*sys.exc_info())
    text = _crash_text(tmp_path)
    assert "uncaught exception" in text and "KeyError: 'main thread'" in text


def test_unraisable_exception_is_recorded(cfg, tmp_path):
    try:
        raise RuntimeError("half-built object")
    except RuntimeError:
        info = sys.exc_info()
    sys.unraisablehook(types.SimpleNamespace(exc_type=info[0], exc_value=info[1],
                                             exc_traceback=info[2], object=None))
    assert "unraisable exception" in _crash_text(tmp_path)
    assert "RuntimeError: half-built object" in _crash_text(tmp_path)


def test_liveness_file_carries_the_state_a_crash_needs(cfg, tmp_path):
    crashguard.tick("eventloop")
    crashguard.note_event("meta_event/heartbeat")
    crashguard.breadcrumb("api GetGroupList")
    deadline = time.monotonic() + 5
    data = None
    while time.monotonic() < deadline:
        try:
            data = json.loads((tmp_path / "alive.json").read_text(encoding="utf-8"))
            break
        except OSError:
            time.sleep(0.1)
    assert data, "the watchdog never wrote the liveness file"
    assert data["pid"] > 0 and "dd-watchdog" in data["threads"]
    assert "dd-log-file" in data["threads"], "listener threads must be identifiable in a dump"
    assert data["last_event"]["what"] == "meta_event/heartbeat"
    assert "eventloop" in data["tick_age_s"]
    assert "api GetGroupList" in data["recent_ops"]
    assert set(data["log_queue"]) == {"file"}


def test_a_stall_dumps_every_thread_stack(tmp_path):
    c = _cfg(tmp_path, stall_s=0.3, liveness_s=0)
    crashguard.install(c)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "STALL" in _crash_text(tmp_path):
                break
            time.sleep(0.2)
        text = _crash_text(tmp_path)
        assert "STALL: nothing ticked for" in text
        assert "--- python stacks of every thread" in text
        assert "dd-watchdog" in text  # the watchdog dumped itself along with everyone else
        assert "Current thread" in text or "Thread 0x" in text
    finally:
        crashguard.stop()


def test_shutdown_is_noted_in_the_crash_log(cfg, tmp_path):
    crashguard.note_shutdown(0)
    assert "driver exiting: clean exit" in _crash_text(tmp_path)


NATIVE_FAULT_SCRIPT = r"""
import ctypes, sys, time
from pathlib import Path
from dicedriver import config, crashguard

cfg = config.from_dict({"log": {
    "crash_file": r"__CRASH__",
    "alive_file": r"__ALIVE__",
    "stderr_file": "",
    "console": False,
    "liveness_s": 0.2,
    "stall_s": 0,
}}, Path(r"__WORK__"))
crashguard.install(cfg)
crashguard.tick("eventloop")
crashguard.note_event("group_message")
crashguard.breadcrumb("api GetGroupList")
time.sleep(1.5)          # let the watchdog publish the liveness file first

def frobnicate():
    # A hard access violation: ctypes translates faults raised *inside one of its calls* into
    # OSError, so the fault has to happen outside a call - exactly like a crash in a Dice thread.
    ctypes.c_char.from_address(1).value = b"x"

frobnicate()
"""


@pytest.mark.skipif(sys.platform != "win32", reason="native fault codes are Windows specific")
def test_native_access_violation_leaves_both_files(tmp_path):
    """The real thing: a fault inside the DLL kills the process, and we still get a stack."""
    crash = tmp_path / "crash.log"
    alive = tmp_path / "alive.json"
    script = (NATIVE_FAULT_SCRIPT.replace("__CRASH__", str(crash))
              .replace("__ALIVE__", str(alive)).replace("__WORK__", str(tmp_path)))
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=90)
    assert proc.returncode != 0, f"the child was supposed to die: {proc.stdout}{proc.stderr}"
    assert crashguard.decode_exit_code(proc.returncode).startswith("ACCESS_VIOLATION")

    text = _crash_text(tmp_path)
    assert "Windows fatal exception" in text
    assert "frobnicate" in text, "no Python frame in the dump"
    assert "driver exiting" not in text, "a run without that line died hard - that is the signal"

    state = json.loads(alive.read_text(encoding="utf-8"))
    assert "api GetGroupList" in state["recent_ops"]
    assert state["last_event"]["what"] == "group_message"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows status codes")
def test_fatal_codes_are_named():
    assert crashguard.decode_exit_code(-1073741819).startswith("ACCESS_VIOLATION")
    assert crashguard.decode_exit_code(-1073740791).startswith("STACK_BUFFER_OVERRUN")
    assert crashguard.decode_exit_code(0xC0000374).startswith("HEAP_CORRUPTION")
    assert crashguard.decode_exit_code(crashguard.RESTART_EXIT_CODE).startswith("supervisor restart")
    assert crashguard.decode_exit_code(0) == "clean exit"
    assert crashguard.decode_exit_code(None) == "still running"
    assert crashguard.decode_exit_code(3) == "exit code 3"


def test_logging_without_install_is_harmless():
    """A unit test or a tool may build an App without arming the guard; nothing may explode."""
    crashguard.stop()
    crashguard.tick("nowhere")
    crashguard.breadcrumb("nothing")
    crashguard.note_event("nothing")
    crashguard.note_shutdown(0)
    assert crashguard.state() is None
    assert logging.getLogger("dd.crash") is not None
