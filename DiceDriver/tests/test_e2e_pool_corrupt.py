# -*- encoding: utf-8 -*-
"""
@File     :   test_e2e_pool_corrupt.py
@Desc     :   池文件不可信时 Dice 必须丢掉它——既不许沿用它，也不许因此崩掉
@Note     :   "没沿用我们那份池"是这里唯一能离线断言的信号：真被丢掉了 Dice 要么不落盘，要么落盘的是
              刚从 random.org 取的新数据；只有"照用了"才会写回 SEED 的一个后缀
"""

from __future__ import annotations

import contextlib
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

SEED = [(i * 7919) % 65536 for i in range(1000)]

# 旧版 v1 明文数组，内容是真的池数据：换格式以后它必须被整份忽略，而不是接着用
V1_POOL = json.dumps({"version": 1, "bitBuf": 0, "bitCnt": 0, "words": SEED})


def v2_pool_with_trailing_junk() -> str:
    """v2 池，但在最后一个分片尾部塞上杂字符。

    base64 解码器遇到字母表外的字符会静默停下，所以这段尾巴解出来仍是完整的正确内容、校验和也照样
    通过——只有"解码结果必须能原样再编码回去"这条判断才拦得住它。
    """
    doc = json.loads(encode_pool(SEED, 0, 0))
    doc["words"][-1] += "!!!!"
    return json.dumps(doc)


@contextlib.contextmanager
def _dice_with_pool(tmp: Path, pool_text: str):
    fake = FakeOneBot()
    fake.start()
    conf_dir = tmp / "data" / f"Dice{BOT_QQ}" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "console.yaml").write_text("config:\n  RandomMode: 1\n", encoding="utf-8")
    (conf_dir / "random_pool.json").write_text(pool_text, encoding="utf-8")
    cfg = tmp / "dicedriver.toml"
    cfg.write_text(f"""
[driver]
dice_dll = '{DICE_DLL}'
shim_dll = '{SHIM}'
root_dir = '{tmp / "data"}'

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
        yield fake, tmp, proc
    finally:
        proc.kill()
        proc.wait(10)


@pytest.fixture(scope="module")
def session_v1(tmp_path_factory):
    with _dice_with_pool(tmp_path_factory.mktemp("e2epoolv1"), V1_POOL) as s:
        yield s


@pytest.fixture(scope="module")
def session_junk(tmp_path_factory):
    with _dice_with_pool(tmp_path_factory.mktemp("e2epooljunk"), v2_pool_with_trailing_junk()) as s:
        yield s


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


def _roll_and_stop(fake, proc) -> None:
    _converse(fake, lambda: fake.group_message(".r d20"), "send_group_msg", "D20")   # 仍然掷得出来
    proc.send_signal(signal.CTRL_BREAK_EVENT)
    assert proc.wait(60) == 0


def _assert_pool_not_adopted(work: Path) -> None:
    path = work / "data" / f"Dice{BOT_QQ}" / "conf" / "random_pool.json"
    if not path.exists():
        return                                     # 没落盘：说明池是空的，本来就没什么可写
    decoded = decode_pool(path.read_text(encoding="utf-8"))
    if decoded is None:
        return                                     # 我们的文件原封不动，或写出来的也不是我们这份
    words = decoded[0]
    assert words != SEED[len(SEED) - len(words):], "Dice 沿用了那份本该被丢弃的池"


def test_old_v1_pool_is_not_read_anymore(session_v1):
    fake, work, proc = session_v1
    _roll_and_stop(fake, proc)
    _assert_pool_not_adopted(work)


def test_trailing_junk_is_rejected(session_junk):
    fake, work, proc = session_junk
    _roll_and_stop(fake, proc)
    _assert_pool_not_adopted(work)
