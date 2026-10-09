"""End-to-end: real Dice DLL + real driver process + scripted OneBot server.

Skipped when the x64 Dice DLL or the shim has not been built.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fake_onebot import BOT_QQ, GROUP, MASTER_QQ, FakeOneBot

ROOT = Path(__file__).resolve().parent.parent
DICE_DLL = Path(os.environ.get("DICE_DLL", r"D:\dice-build\Release\w4123.Dice.windows.amd64.dll"))
SHIM = ROOT / "bin" / "dd_shim.dll"

pytestmark = pytest.mark.skipif(not (DICE_DLL.is_file() and SHIM.is_file()),
                                reason="needs the built Dice DLL and bin/dd_shim.dll")


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    work = tmp_path_factory.mktemp("e2e")
    fake = FakeOneBot()
    fake.start()
    conf_dir = work / "data" / f"Dice{BOT_QQ}" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "console.yaml").write_text("config:\n  EnableWebUI: 0\n", encoding="utf-8")
    cfg = work / "dicedriver.toml"
    cfg.write_text(f"""
[driver]
dice_dll = '{DICE_DLL}'
shim_dll = '{SHIM}'
root_dir = '{work / "data"}'
state_dir = '{work / "state"}'

[onebot]
mode = "forward"
url = "ws://127.0.0.1:{fake.port}"

[log]
level = "trace"
console = false
""", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "dicedriver", "--config", str(cfg)], cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        assert fake.connected.wait(20), "driver never connected"
        yield fake, work, proc
    finally:
        proc.kill()
        proc.wait(10)


def _converse(fake, send, action, contains, timeout=40.0):
    """Events sent before Dice finished starting are dropped, so retry until a reply shows up."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        send()
        try:
            return fake.wait_for(action, contains, timeout=2.5)
        except AssertionError:
            continue
    raise AssertionError(f"no {action} with {contains!r}")


def test_group_bot_command_replies_with_driver_version(session):
    fake, *_ = session
    reply = _converse(fake, lambda: fake.group_message(".bot"), "send_group_msg", "DiceDriver-py")
    assert reply["group_id"] == GROUP
    text = "".join(seg["data"].get("text", "") for seg in reply["message"])
    assert "OneBot11" in text


def test_private_bot_command_replies(session):
    fake, *_ = session
    reply = _converse(fake, lambda: fake.private_message(".bot"), "send_private_msg", "DiceDriver-py")
    assert reply["user_id"] == MASTER_QQ


def test_dice_roll_in_group(session):
    fake, *_ = session
    before = len(fake.find("send_group_msg"))
    fake.group_message(".r d20")
    end = time.monotonic() + 15
    while time.monotonic() < end and len(fake.find("send_group_msg")) <= before:
        time.sleep(0.2)
    new = fake.find("send_group_msg")[before:]
    assert new, "Dice produced no reply to .r d20"
    assert "D20" in "".join(seg["data"].get("text", "") for seg in new[-1]["message"]).upper()


def test_startup_pulled_state_and_wrote_logs(session):
    fake, work, _ = session
    for action in ("get_login_info", "get_group_list", "get_friend_list"):
        assert fake.find(action), action
    log = work / "logs" / "dicedriver.log"
    assert log.exists()
    assert "Dice started" in log.read_text(encoding="utf-8")
    dice_log = log.parent / f"dice-{BOT_QQ}.log"
    assert dice_log.exists() and f"Dice{BOT_QQ}.init" in dice_log.read_text(encoding="utf-8")


def test_group_invite_is_answered_with_cached_flag(session):
    fake, *_ = session
    _converse(fake, lambda: fake.group_message(".bot"), "send_group_msg", "DiceDriver-py")  # Dice is up
    fake.push({"post_type": "request", "request_type": "group", "sub_type": "invite", "group_id": 30001,
               "user_id": MASTER_QQ, "flag": "INV-FLAG-1", "comment": ""})
    answer = fake.wait_for("set_group_add_request", "INV-FLAG-1", timeout=20)
    assert answer["sub_type"] == "invite"
    assert isinstance(answer["approve"], bool)


def test_friend_request_is_answered_with_cached_flag(session):
    fake, *_ = session
    fake.push({"post_type": "request", "request_type": "friend", "user_id": 777, "comment": "hi",
               "flag": "FR-FLAG-1"})
    answer = fake.wait_for("set_friend_add_request", "FR-FLAG-1", timeout=20)
    assert isinstance(answer["approve"], bool)


def test_graceful_stop_flushes_state_and_exits_zero(session):
    import signal
    fake, work, proc = session
    fake.group_message("flush me")  # records a last-speak entry
    time.sleep(0.5)
    proc.send_signal(signal.CTRL_BREAK_EVENT)
    assert proc.wait(60) == 0
    log = (work / "logs" / "dicedriver.log").read_text(encoding="utf-8")
    assert "stopping Dice" in log and "driver exiting (code 0)" in log
    state = work / "state" / "last_speak.json"
    assert state.exists() and str(MASTER_QQ) in state.read_text(encoding="utf-8")

