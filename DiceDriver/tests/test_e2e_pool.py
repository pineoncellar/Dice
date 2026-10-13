# -*- encoding: utf-8 -*-
"""
@File     :   test_e2e_pool.py
@Desc     :   真随机池的端到端往返：预置一个 Python 写出来的 v2 池文件，让 Dice 读它、用掉几个字、再写回来
@Note     :   DiceSrc 没有 C++ 测试骨架，RandomGenerator 的编解码又在匿名命名空间里，所以这里的
              "写回序列是预置序列的一个后缀"是唯一能同时证明读得进、没去取数、又写得对的手段
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fake_onebot import BOT_QQ, FakeOneBot
from pool_format import decode_pool, encode_pool

ROOT = Path(__file__).resolve().parent.parent          # <workspace>\DiceDriver
WS = ROOT.parent                                       # workspace root
DICE_DLL = Path(os.environ.get("DICE_DLL", WS / "output" / "w4123.Dice.windows.amd64.dll"))
SHIM = WS / "output" / "dd_shim.dll"

pytestmark = pytest.mark.skipif(not (DICE_DLL.is_file() and SHIM.is_file()),
                                reason="needs output\\w4123.Dice.windows.amd64.dll and output\\dd_shim.dll")

# 1000 字 = 16000 位，远高于补充阈值 4000，所以启动时不会去 random.org 取数
SEED = [(i * 7919) % 65536 for i in range(1000)]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    work = tmp_path_factory.mktemp("e2epool")
    fake = FakeOneBot()
    fake.start()
    conf_dir = work / "data" / f"Dice{BOT_QQ}" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "console.yaml").write_text("config:\n  RandomMode: 1\n", encoding="utf-8")
    (conf_dir / "random_pool.json").write_text(encode_pool(SEED, 0, 0), encoding="utf-8")
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


def test_pool_survives_a_round_trip_through_dice(session):
    fake, work, proc = session
    _converse(fake, lambda: fake.group_message(".r d20"), "send_group_msg", "D20")
    proc.send_signal(signal.CTRL_BREAK_EVENT)
    assert proc.wait(60) == 0                                 # 优雅停止会走 global_exit -> Save
    text = (work / "data" / f"Dice{BOT_QQ}" / "conf" / "random_pool.json").read_text(encoding="utf-8")
    assert json.loads(text)["version"] == 2
    words, _bit_buf, bit_cnt = decode_pool(text)               # 解不开会返回 None，拆包即失败
    assert 0 <= bit_cnt < 16
    assert len(words) < len(SEED)                              # 掷骰消耗掉了至少一个字
    assert words == SEED[len(SEED) - len(words):]              # 且恰好是预置序列的一个后缀
