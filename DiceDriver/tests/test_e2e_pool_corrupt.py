# -*- encoding: utf-8 -*-
"""
@File     :   test_e2e_pool_corrupt.py
@Desc     :   池文件是垃圾时 Dice 仍然要能起、能掷、能干净退出
@Note     :   池被判空后 Dice 会在后台线程去 random.org 补充，失败就退避降级，所以这个用例不依赖网络
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

# 旧格式的整数数组，外加一个 v2 解不开的 version
GARBAGE = '{"version": 1, "bitBuf": 0, "bitCnt": 0, "words": [1, 2, 3]}'


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    work = tmp_path_factory.mktemp("e2epoolbad")
    fake = FakeOneBot()
    fake.start()
    conf_dir = work / "data" / f"Dice{BOT_QQ}" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "console.yaml").write_text("config:\n  RandomMode: 1\n", encoding="utf-8")
    (conf_dir / "random_pool.json").write_text(GARBAGE, encoding="utf-8")
    cfg = work / "dicedriver.toml"
    cfg.write_text(f"""
[driver]
dice_dll = '{DICE_DLL}'
shim_dll = '{SHIM}'
root_dir = '{work / "data"}'

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


def test_garbage_pool_does_not_break_dice(session):
    fake, work, proc = session
    _converse(fake, lambda: fake.group_message(".r d20"), "send_group_msg", "D20")   # 仍然掷得出来
    proc.send_signal(signal.CTRL_BREAK_EVENT)
    assert proc.wait(60) == 0
