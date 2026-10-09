"""Reload/Remake: Dice asks the host to restart; the driver replaces its own process."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from fake_onebot import BOT_QQ, MASTER_QQ, FakeOneBot
from test_e2e import DICE_DLL, ROOT, SHIM

pytestmark = pytest.mark.skipif(not (DICE_DLL.is_file() and SHIM.is_file()),
                                reason="needs the built Dice DLL and bin/dd_shim.dll")


def _kill_by_cmdline(fragment: str) -> None:
    script = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{fragment}*' "
              f"-and $_.ProcessId -ne $PID }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False, timeout=60)


def test_admin_reload_restarts_the_driver_process(tmp_path):
    fake = FakeOneBot()
    fake.start()
    conf_dir = tmp_path / "data" / f"Dice{BOT_QQ}" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "console.yaml").write_text(f"master: {MASTER_QQ}\nconfig:\n  EnableWebUI: 0\n", encoding="utf-8")
    cfg = tmp_path / "dicedriver.toml"
    cfg.write_text(f"""
[driver]
dice_dll = '{DICE_DLL}'
shim_dll = '{SHIM}'
root_dir = '{tmp_path / "data"}'
state_dir = '{tmp_path / "state"}'

[onebot]
url = "ws://127.0.0.1:{fake.port}"

[log]
console = false
""", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "dicedriver", "--config", str(cfg)], cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert fake.connected.wait(20)
        end = time.monotonic() + 40
        while time.monotonic() < end and not fake.find("send_private_msg"):
            fake.private_message(".bot")
            time.sleep(2)
        assert fake.find("send_private_msg"), "Dice never answered"

        logins_before = len(fake.find("get_login_info"))
        fake.private_message(".system reload")
        assert proc.wait(60) == 0, "the old driver process should exit after handing over"
        end = time.monotonic() + 40
        while time.monotonic() < end and len(fake.find("get_login_info")) <= logins_before:
            time.sleep(0.3)
        assert len(fake.find("get_login_info")) > logins_before, "the replacement process never reconnected"
        # the replacement is a fully working driver again
        end = time.monotonic() + 40
        sent = len(fake.find("send_private_msg"))
        while time.monotonic() < end and len(fake.find("send_private_msg")) <= sent:
            fake.private_message(".bot")
            time.sleep(2)
        assert len(fake.find("send_private_msg")) > sent
    finally:
        proc.kill()
        _kill_by_cmdline(tmp_path.name)


