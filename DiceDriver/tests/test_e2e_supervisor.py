"""End-to-end: the real driver under the real supervisor, with the real Dice DLL.

Skipped when the x64 Dice DLL or the shim has not been built.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fake_onebot import BOT_QQ, FakeOneBot

ROOT = Path(__file__).resolve().parent.parent          # <workspace>\DiceDriver
WS = ROOT.parent                                       # workspace root
DICE_DLL = Path(os.environ.get("DICE_DLL", WS / "output" / "w4123.Dice.windows.amd64.dll"))
SHIM = WS / "output" / "dd_shim.dll"

pytestmark = pytest.mark.skipif(not (DICE_DLL.is_file() and SHIM.is_file()),
                                reason="needs output\\w4123.Dice.windows.amd64.dll and output\\dd_shim.dll")


class Supervised:
    """One supervisor + driver pair, started through the command line the launcher uses."""

    def __init__(self, work: Path, fake: FakeOneBot, proc: subprocess.Popen):
        self.work = work
        self.fake = fake
        self.proc = proc

    @property
    def log(self) -> str:
        return (self.work / "logs" / "supervisor.log").read_text(encoding="utf-8")

    @property
    def incidents(self) -> list[Path]:
        return sorted((self.work / "logs" / "incidents").glob("*.txt"))

    def wait_for_child_count(self, count: int, timeout: float = 90) -> str:
        """The restart line is logged before the replacement is spawned; wait for the spawn itself."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.log
            if text.count("child started") >= count:
                return text
            time.sleep(0.5)
        raise AssertionError(f"the supervisor never started {count} children:\n{self.log}")

    def wait_for_dice(self, timeout: float = 150) -> dict:
        """Wait until the child reports that Dice itself is up (the OneBot link is up much earlier)."""
        deadline = time.monotonic() + timeout
        log_path = self.work / "logs" / "dicedriver.log"
        while time.monotonic() < deadline:
            try:
                text = log_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if "Dice started in" in text:
                return self.fake.wait_for("get_group_list", timeout=10)
            time.sleep(0.5)
        raise AssertionError(f"Dice never reported a start:\n{text[-2000:]}")

    def driver_pid(self) -> int:
        """The pid the supervisor spawned (read from its log, so a launcher shim cannot confuse us)."""
        import re
        match = re.search(r"child started \(pid (\d+)\)", self.log)
        assert match, f"the supervisor never reported a child:\n{self.log}"
        return int(match.group(1))

    def stop(self) -> None:
        if self.proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)
        self.proc.wait(15)


def _start(tmp_path: Path, fake: FakeOneBot) -> Supervised:
    work = tmp_path / "supervised"
    work.mkdir()
    conf_dir = work / "data" / f"Dice{BOT_QQ}" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "console.yaml").write_text("config:\n  EnableWebUI: 0\n", encoding="utf-8")
    (work / "dicedriver.toml").write_text(f"""
[driver]
dice_dll = '{DICE_DLL}'
shim_dll = '{SHIM}'
root_dir = '{work / "data"}'

[onebot]
mode = "forward"
url = "ws://127.0.0.1:{fake.port}"

[log]
level = "info"
console = false
stderr_file = ""
""", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "dicedriver", "--config",
                             str(work / "dicedriver.toml"), "--supervise",
                             "--restart-delay", "0", "--max-restarts", "1"],
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    assert fake.connected.wait(60), "the supervised driver never connected"
    return Supervised(work, fake, proc)


@pytest.fixture
def session(tmp_path):
    fake = FakeOneBot()
    fake.start()
    sup = _start(tmp_path, fake)
    try:
        yield sup
    finally:
        sup.stop()


def test_a_clean_stop_is_not_restarted(session):
    session.wait_for_dice()
    session.proc.send_signal(signal.CTRL_BREAK_EVENT)

    assert session.proc.wait(90) == 0, session.proc.stdout.read().decode("utf-8", "replace")
    text = session.log
    assert text.count("child started") == 1, "a clean stop must not be answered with a restart"
    assert "not restarting" in text
    assert not session.incidents, "a clean stop must not produce an incident report"

    # the child armed its own forensics and stopped in an orderly way
    driver = (session.work / "logs" / "dicedriver.log").read_text(encoding="utf-8")
    assert "crash forensics armed" in driver
    assert "stopping Dice" in driver and "driver exiting (code 0:" in driver
    assert (session.work / "logs" / "journal.log").exists()
    assert (session.work / "logs" / "alive.json").exists()
    crash = (session.work / "logs" / "crash.log").read_text(encoding="utf-8")
    assert "driver started" in crash and "driver exiting" in crash


def test_a_killed_child_gets_a_report_and_a_replacement(session):
    """taskkill /F is the closest thing to a native crash: the child dies without a word."""
    session.wait_for_dice()
    killed = subprocess.run(["taskkill", "/F", "/T", "/PID", str(session.driver_pid())],
                            capture_output=True, text=True)
    assert killed.returncode == 0, killed.stderr

    text = session.wait_for_child_count(2)
    assert "ABNORMAL EXIT" in text, "an abnormal exit has to be reported"
    incidents = session.incidents
    assert incidents, "a death with no exit code of its own still has to produce a report"
    body = incidents[0].read_text(encoding="utf-8")
    assert "exit code   :" in body
    assert "LocalDumps" in body, "the report has to say how to get a native stack next time"
    assert "--- tail of" in body
