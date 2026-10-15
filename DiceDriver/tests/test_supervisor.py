"""Supervisor: a child's exit code is always seen, always written down, and answered with a restart."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from dicedriver import app as app_module
from dicedriver import config, crashguard
from dicedriver.supervisor import Supervisor

ROOT = Path(__file__).resolve().parent.parent  # <workspace>\DiceDriver


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _supervisor(tmp_path, child, **kw):
    kw.setdefault("restart_delay", 0.0)
    kw.setdefault("alive_file", tmp_path / "alive.json")
    kw.setdefault("tail_files", [tmp_path / "crash.log"])
    return Supervisor(child, tmp_path / "logs", cwd=str(ROOT), **kw)


def _log(tmp_path) -> str:
    return (tmp_path / "logs" / "supervisor.log").read_text(encoding="utf-8")


def test_abnormal_exit_is_recorded_and_restarted(tmp_path):
    sup = _supervisor(tmp_path, _child("import sys; sys.exit(3)"), max_restarts=1)
    assert sup.run() == 3
    text = _log(tmp_path)
    assert text.count("child started") == 2, "the first death should have been answered with a restart"
    assert "ABNORMAL EXIT" in text and "exit code 3" in text
    assert "CRASH LOOP" in text
    incidents = list((tmp_path / "logs" / "incidents").glob("incident-*.txt"))
    assert len(incidents) == 2
    body = incidents[0].read_text(encoding="utf-8")
    assert "exit code   : 3 (0x00000003)" in body
    assert "meaning     : exit code 3" in body
    assert "uptime      :" in body
    assert "--- tail of" in body
    assert "LocalDumps" in body, "the report must say how to get a native stack next time"


def test_clean_exit_is_not_restarted(tmp_path):
    sup = _supervisor(tmp_path, _child("pass"))
    assert sup.run() == 0
    text = _log(tmp_path)
    assert text.count("child started") == 1
    assert "clean exit: not restarting" in text


def test_a_config_error_is_not_restarted(tmp_path):
    """Exit code 2 is the driver's 'fix your config' code: restarting would just spin."""
    sup = _supervisor(tmp_path, _child("import sys; sys.exit(2)"))
    assert sup.run() == 2
    text = _log(tmp_path)
    assert text.count("child started") == 1
    assert "fix that first, not restarting" in text


def test_the_reload_exit_code_asks_for_an_immediate_restart(tmp_path):
    sup = _supervisor(tmp_path, _child(f"import sys; sys.exit({crashguard.RESTART_EXIT_CODE})"),
                      max_restarts=1)
    assert sup.run() == crashguard.RESTART_EXIT_CODE
    text = _log(tmp_path)
    assert text.count("child started") == 2
    assert "supervisor restart requested" in text
    assert "restarting now" in text  # no delay for a requested restart


def test_restart_can_be_turned_off(tmp_path):
    sup = _supervisor(tmp_path, _child("import sys; sys.exit(9)"), restart=False)
    assert sup.run() == 9
    assert "restart is off; stopping here" in _log(tmp_path)


def test_a_stalled_child_is_killed_and_reported(tmp_path):
    sup = _supervisor(tmp_path, _child("import time; time.sleep(30)"), restart=False,
                      stall_timeout=0.4)
    started = time.monotonic()
    assert sup.run() == 1
    assert time.monotonic() - started < 25
    text = _log(tmp_path)
    assert "STALL" in text and "killing pid" in text
    assert "killed by the supervisor" in text
    assert "killed by the supervisor" in list((tmp_path / "logs" / "incidents").glob("*.txt"))[0]\
        .read_text(encoding="utf-8")


def test_a_fresh_child_is_not_mistaken_for_a_stall(tmp_path):
    """A liveness file left behind by the previous run must not make the new child look dead."""
    stale = tmp_path / "alive.json"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    sup = _supervisor(tmp_path, _child("import time; time.sleep(1.2)"), restart=False,
                      stall_timeout=5.0, alive_file=stale)
    assert sup.run() == 0
    assert "STALL" not in _log(tmp_path)


def test_supervisor_marks_the_child_as_supervised(tmp_path, monkeypatch):
    """Under a supervisor the driver must hand the restart over instead of forking its own copy."""
    code = _child("import os, sys; sys.exit(0 if os.environ.get('DICEDRIVER_SUPERVISED') == '1' else 4)")
    sup = _supervisor(tmp_path, code)
    assert sup.run() == 0


def test_a_supervised_reload_exits_with_the_restart_code(tmp_path, monkeypatch):
    """Under a supervisor the driver must hand the restart over instead of forking its own copy."""
    cfg = config.from_dict({"driver": {"dice_dll": "x.dll", "shim_dll": "y.dll"}}, tmp_path)
    instance = app_module.App(cfg, tmp_path / "dicedriver.toml")
    seen: list[int] = []
    monkeypatch.setenv(crashguard.SUPERVISED_ENV, "1")
    monkeypatch.setattr(instance, "_stop_dice", lambda timeout: None)
    monkeypatch.setattr(instance, "_finish", seen.append)
    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(app_module.subprocess, "Popen", _no_popen)
    instance._restart_worker("reload")
    assert seen == [crashguard.RESTART_EXIT_CODE]


def _no_popen(*a, **kw):  # pragma: no cover - only reached when supervision is ignored
    raise AssertionError("a supervised driver must not fork its own replacement")
