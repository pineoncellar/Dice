"""End-to-end in reverse mode: the driver listens, the (fake) OneBot implementation dials in."""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

from fake_onebot import BOT_QQ, GROUP, FakeOneBot
from test_e2e import DICE_DLL, ROOT, SHIM, _converse

pytestmark = pytest.mark.skipif(not (DICE_DLL.is_file() and SHIM.is_file()),
                                reason="needs the built Dice DLL and bin/dd_shim.dll")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch(work, port, token=""):
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
mode = "reverse"
host = "127.0.0.1"
port = {port}
path = "/onebot/v11/ws"
access_token = "{token}"
send_format = "string"

[log]
console = false
""", encoding="utf-8")
    return subprocess.Popen([sys.executable, "-m", "dicedriver", "--config", str(cfg)], cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_reverse_connection_with_token_and_string_format(tmp_path):
    port = _free_port()
    proc = _launch(tmp_path, port, token="s3cret")
    try:
        time.sleep(1.5)
        bad = FakeOneBot(reverse_url=f"ws://127.0.0.1:{port}/onebot/v11/ws")
        bad.start()
        assert not bad.connected.wait(3), "a connection without the token must be rejected"

        good = FakeOneBot(reverse_url=f"ws://127.0.0.1:{port}/onebot/v11/ws?access_token=s3cret")
        good.start()
        assert good.connected.wait(10), "authorised reverse connection was not accepted"
        reply = _converse(good, lambda: good.group_message(".bot", as_array=False),
                          "send_group_msg", "DiceDriver-py")
        assert reply["group_id"] == GROUP
        assert isinstance(reply["message"], str)  # send_format = "string"
    finally:
        proc.kill()
        proc.wait(10)

