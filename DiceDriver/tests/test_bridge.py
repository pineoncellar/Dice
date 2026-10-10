"""Relay bridge: the TRPG-state gate (unit) and the OneBot relay endpoint (end-to-end)."""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from pathlib import Path

import pytest

from fake_onebot import BOT_QQ, GROUP, MASTER_QQ, FakeOneBot

from dicedriver.bridge import LOGSTATE_MARK, LogGate, RelayBridge, exposure_warning
from dicedriver.config import BridgeCfg

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT.parent
DICE_DLL = WS / "output" / "w4123.Dice.windows.amd64.dll"
SHIM = WS / "output" / "dd_shim.dll"

needs_dll = pytest.mark.skipif(not (DICE_DLL.is_file() and SHIM.is_file()),
                               reason="needs output\\w4123.Dice.windows.amd64.dll and output\\dd_shim.dll")


# ====================================================================== unit: LogGate

def _line(action: str, on: int = 0, gid: int = 0, uid: int = 0, chid: int = 0, name: str = "") -> str:
    return (f"{LOGSTATE_MARK} v1 on={on} action={action} session=s gid={gid} uid={uid} "
            f"chid={chid} name={name}")


def test_parse_extracts_fields_and_keeps_spaced_names():
    got = LogGate.parse(_line("new", on=1, gid=20001, name="my log name"))
    assert got["action"] == "new" and got["on"] == "1" and got["gid"] == "20001"
    assert got["name"] == "my log name"
    assert LogGate.parse("[something else] on=1") is None
    assert LogGate.parse(f"{LOGSTATE_MARK} v2 on=1 action=new") is None


def test_unknown_is_gated_before_the_first_snapshot():
    gate = LogGate("closed")
    assert gate.gated(("g", 20001)) is True


def test_open_policy_never_gates_when_unknown():
    gate = LogGate("open")
    assert gate.gated(("g", 20001)) is False


def test_incremental_reports_do_not_end_the_unknown_state():
    gate = LogGate("closed")
    assert gate.feed(_line("new", on=1, gid=7)) is True
    assert gate.gated(("g", 7)) is True
    # One report proves the channel works, but it is not the full picture, so every *other*
    # window is still unknown and must not be treated as idle.
    assert gate.gated(("g", 8)) is True
    # snapshot-end is authoritative and replaces the table wholesale, so an incremental
    # received before it does not survive. (In practice Dice always sends the snapshot
    # first: it is emitted while Dice boots, before any command can be processed.)
    assert gate.feed(_line("snapshot-end")) is True
    assert gate.gated(("g", 7)) is False
    assert gate.gated(("g", 8)) is False
    # ...but an incremental received *after* the snapshot is honoured.
    gate.feed(_line("on", on=1, gid=8))
    assert gate.gated(("g", 8)) is True


def test_snapshot_is_authoritative_and_clears_stale_entries():
    gate = LogGate("closed")
    gate.feed(_line("new", on=1, gid=7))
    gate.feed(_line("snapshot", on=1, gid=9))
    gate.feed(_line("snapshot-end"))
    assert gate.gated(("g", 7)) is False, "a window absent from the snapshot must not stay gated"
    assert gate.gated(("g", 9)) is True


def test_off_then_on_toggles_a_single_window():
    gate = LogGate("closed")
    gate.feed(_line("snapshot-end"))
    assert gate.gated(("g", 7)) is False
    gate.feed(_line("on", on=1, gid=7))
    assert gate.gated(("g", 7)) is True
    gate.feed(_line("off", on=0, gid=7))
    assert gate.gated(("g", 7)) is False


def test_private_and_channel_windows_use_their_own_keys():
    gate = LogGate("closed")
    gate.feed(_line("snapshot", on=1, uid=555))
    gate.feed(_line("snapshot", on=1, chid=666))
    gate.feed(_line("snapshot-end"))
    assert gate.gated(("u", 555)) is True
    assert gate.gated(("c", 666)) is True
    assert gate.gated(("g", 555)) is False, "a private window must not gate the same-numbered group"


def test_grace_period_releases_the_closed_default():
    gate = LogGate("closed", grace_s=0.0)
    gate.arm()
    assert gate.gated(("g", 7)) is False, "without any report the gate must give up, not mute forever"


def test_gate_accepts_string_and_int_ids_interchangeably():
    gate = LogGate("closed")
    gate.feed(_line("snapshot", on=1, gid=7))
    gate.feed(_line("snapshot-end"))
    assert gate.gated(("g", 7)) is True


# ====================================================================== unit: routing decisions

def _bridge(gate: LogGate, **kw) -> RelayBridge:
    cfg = BridgeCfg(**kw)
    return RelayBridge(cfg, link=None, gate=gate)  # type: ignore[arg-type]


def test_only_message_actions_have_a_target():
    br = _bridge(LogGate("closed"))
    assert br.target_of("send_group_msg", {"group_id": 20001}) == ("g", 20001)
    assert br.target_of("send_group_forward_msg", {"group_id": 20001}) == ("g", 20001)
    assert br.target_of("send_msg", {"group_id": 20001}) == ("g", 20001)
    assert br.target_of("send_msg", {"message_type": "group", "group_id": 1}) == ("g", 1)
    # query / management actions are never gated
    for action in ("get_group_list", "get_group_member_list", "set_group_ban", "delete_msg",
                   "get_msg", "set_group_kick", "send_like", "get_login_info"):
        assert br.target_of(action, {}) is None, action


def test_private_targets_are_gated_only_when_opted_in():
    off = _bridge(LogGate("closed"))
    assert off.target_of("send_private_msg", {"user_id": 5}) is None
    assert off.target_of("send_msg", {"user_id": 5}) is None
    on = _bridge(LogGate("closed"), gate_private=True)
    assert on.target_of("send_private_msg", {"user_id": 5}) == ("u", 5)
    assert on.target_of("send_msg", {"user_id": 5}) == ("u", 5)


def test_message_sent_is_never_relayed_by_default():
    gate = LogGate("closed")
    gate.feed(_line("snapshot-end"))
    br = _bridge(gate)
    assert br._withheld({"post_type": "message_sent", "message_type": "group", "group_id": 1})
    opted_in = _bridge(gate, relay_self_messages=True)
    assert opted_in._withheld({"post_type": "message_sent", "message_type": "group", "group_id": 1}) == ""
    # ...but opting in must not leak a recording group either.
    gate.feed(_line("on", on=1, gid=20001))
    assert opted_in._withheld({"post_type": "message_sent", "message_type": "group",
                               "group_id": 20001}) == "TRPG session"


def test_events_are_withheld_only_for_recording_groups():
    gate = LogGate("closed")
    gate.feed(_line("snapshot", on=1, gid=20001))
    gate.feed(_line("snapshot-end"))
    br = _bridge(gate)
    assert br._withheld({"post_type": "message", "message_type": "group", "group_id": 20001}) == "TRPG session"
    assert br._withheld({"post_type": "message", "message_type": "group", "group_id": 999}) == ""
    assert br._withheld({"post_type": "message", "message_type": "private", "user_id": 5}) == ""
    # notices/requests are not messages and must always pass
    assert br._withheld({"post_type": "notice", "notice_type": "group_increase", "group_id": 20001}) == ""
    assert br._withheld({"post_type": "request", "request_type": "friend", "group_id": 20001}) == ""


# ====================================================================== unit: token / path auth

class _Conn:
    def __init__(self):
        self.answered: tuple | None = None

    def respond(self, status, body):
        self.answered = (status, body)
        return "reject"


class _Req:
    def __init__(self, path: str, headers: dict | None = None):
        self.path = path
        self.headers = headers or {}


def _authorize(br: RelayBridge, path: str = "/", headers: dict | None = None):
    conn, req = _Conn(), _Req(path, headers)
    return br._authorize(conn, req), conn


def test_relay_requires_the_token_when_one_is_configured():
    br = _bridge(LogGate("closed"), access_token="s3cret")
    # accepted: standard Bearer, the Token scheme the upstream also takes, and the query form
    assert _authorize(br)[0] == "reject", "no credentials must be refused"
    for headers in ({"Authorization": "Bearer s3cret"}, {"Authorization": "Token s3cret"}):
        assert _authorize(br, headers=headers)[0] is None, headers
    assert _authorize(br, "/?access_token=s3cret")[0] is None
    assert _authorize(br, "/?x=1&access_token=s3cret")[0] is None
    # refused
    for headers in ({"Authorization": "Bearer nope"}, {"Authorization": "Token nope"},
                    {"Authorization": "s3cret"}, {"Authorization": "Basic czNjcmV0"},
                    {"Authorization": "Bearer "}):
        verdict, conn = _authorize(br, headers=headers)
        assert verdict == "reject", headers
        assert conn.answered[0] == HTTPStatus.UNAUTHORIZED, headers
    assert _authorize(br, "/?access_token=nope")[0] == "reject"


def test_relay_without_a_token_accepts_any_client():
    br = _bridge(LogGate("closed"))
    assert _authorize(br)[0] is None
    assert _authorize(br, headers={"Authorization": "Bearer whatever"})[0] is None


def test_relay_enforces_its_path():
    br = _bridge(LogGate("closed"), path="/ob")
    assert _authorize(br, "/ob")[0] is None
    assert _authorize(br, "/ob/")[0] is None
    assert _authorize(br, "/ob?x=1")[0] is None
    verdict, conn = _authorize(br, "/other")
    assert verdict == "reject" and conn.answered[0] == HTTPStatus.NOT_FOUND


def test_exposure_warning_fires_only_for_tokenless_non_loopback_hosts():
    assert exposure_warning("127.0.0.1", "") == ""
    assert exposure_warning("localhost", "") == ""
    assert exposure_warning("::1", "") == ""
    assert exposure_warning("0.0.0.0", "s3cret") == ""
    assert exposure_warning("192.168.1.10", "s3cret") == ""
    assert "access_token" in exposure_warning("0.0.0.0", "")
    assert "access_token" in exposure_warning("192.168.1.10", "")


# ====================================================================== end-to-end

class MlBot:
    """A minimal OneBot client standing in for 麦bot (connects out, like it would to SnowLuma)."""

    def __init__(self, token: str = ""):
        self.events: list[dict] = []
        self.responses: dict[str, dict] = {}
        self.connected = threading.Event()
        self._ws = None
        self._loop = asyncio.new_event_loop()
        self._echo = itertools.count(1)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._url = ""
        self._token = token
        self._sendq: asyncio.Queue = asyncio.Queue()

    def start(self, url: str) -> None:
        self._url = url
        self._thread.start()
        assert self.connected.wait(10), "麦bot never connected to the relay"

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        from websockets.asyncio.client import connect
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        while True:
            try:
                async with connect(self._url, additional_headers=headers, max_size=None) as ws:
                    self._ws = ws
                    self.connected.set()
                    reader = asyncio.create_task(self._read(ws))
                    writer = asyncio.create_task(self._write(ws))
                    await reader
                    writer.cancel()
            except Exception:
                await asyncio.sleep(0.2)

    async def _read(self, ws) -> None:
        async for raw in ws:
            data = json.loads(raw)
            if "echo" in data and data["echo"] is not None:
                self.responses[str(data["echo"])] = data
            else:
                self.events.append(data)

    async def _write(self, ws) -> None:
        while True:
            frame = await self._sendq.get()
            await ws.send(json.dumps(frame, ensure_ascii=False))

    def request(self, action: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        echo = str(next(self._echo))
        asyncio.run_coroutine_threadsafe(
            self._sendq.put({"action": action, "params": params or {}, "echo": echo}), self._loop).result(5)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if echo in self.responses:
                return self.responses.pop(echo)
            time.sleep(0.05)
        raise AssertionError(f"no response to {action} (echo={echo})")

    def group_events(self, gid: int) -> list[dict]:
        return [e for e in self.events
                if e.get("post_type") == "message" and e.get("group_id") == gid]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


RELAY_TOKEN = "s3cret-relay"


def _try_connect(url: str, token: str | None) -> bool:
    """One-shot handshake attempt; False when the relay refuses it (websockets raises)."""
    from websockets.asyncio.client import connect

    async def attempt() -> bool:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with connect(url, additional_headers=headers, max_size=None, open_timeout=5):
                return True
        except Exception:
            return False

    return asyncio.run(attempt())


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    work = tmp_path_factory.mktemp("relay")
    dice_dir = work / "data" / f"Dice{BOT_QQ}"
    (dice_dir / "conf").mkdir(parents=True)
    (dice_dir / "conf" / "console.yaml").write_text("config:\n  EnableWebUI: 0\n", encoding="utf-8")

    upstream = FakeOneBot()
    upstream.start()
    port = _free_port()
    cfg = work / "dicedriver.toml"
    cfg.write_text(f"""
[driver]
dice_dll = '{DICE_DLL}'
shim_dll = '{SHIM}'
root_dir = '{work / "data"}'
state_dir = '{work / "state"}'

[onebot]
mode = "forward"
url = "ws://127.0.0.1:{upstream.port}"

[bridge]
enabled = true
host = "127.0.0.1"
port = {port}
access_token = "{RELAY_TOKEN}"

[log]
level = "trace"
console = false
""", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "dicedriver", "--config", str(cfg)], cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    ml = MlBot(RELAY_TOKEN)
    try:
        assert upstream.connected.wait(20), "driver never connected upstream"
        ml.start(f"ws://127.0.0.1:{port}/")
        yield upstream, ml, work, f"ws://127.0.0.1:{port}/"
    finally:
        proc.kill()
        proc.wait(10)


def _wait(predicate, timeout: float = 30.0, what: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _dice_up(upstream: FakeOneBot) -> None:
    """Wait for Dice to finish booting.

    NB: keep this gentle — Dice ignores a chat after ~20 commands in 30s (monitorFrq), and a
    tight resend loop trips exactly that.
    """
    end = time.monotonic() + 60.0
    while time.monotonic() < end:
        skip = len(upstream.find("send_group_msg", "DiceDriver-py"))
        upstream.group_message(".bot")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if len(upstream.find("send_group_msg", "DiceDriver-py")) > skip:
                return
            time.sleep(0.5)
    raise AssertionError("Dice never came up")


@needs_dll
def test_relay_enforces_its_token_over_a_real_handshake(relay):
    _, _, _, url = relay
    assert _try_connect(url, None) is False, "an unauthenticated client was accepted"
    assert _try_connect(url, "wrong") is False, "a client with a bad token was accepted"
    assert _try_connect(url, RELAY_TOKEN) is True, "the configured token was refused"


@needs_dll
def test_relay_end_to_end(relay):
    upstream, ml, _, _ = relay
    _dice_up(upstream)
    upstream.group_message(".bot")  # a real group message for the relay to consider

    # 1) Idle: group messages reach 麦bot, and the snapshot has therefore completed.
    _wait(lambda: ml.group_events(GROUP), what="a relayed group message before .log")

    # 2) Queries pass through untouched.
    groups = ml.request("get_group_list")
    assert groups["status"] != "failed" and groups["data"], groups

    # 3) send_group_msg passes through, and 麦bot gets its own echo back, not ours.
    before = len(upstream.find("send_group_msg", "relay-hello"))
    reply = ml.request("send_group_msg", {"group_id": GROUP, "message": "relay-hello"})
    assert reply["status"] != "failed", reply
    assert reply.get("echo") is not None
    _wait(lambda: len(upstream.find("send_group_msg", "relay-hello")) > before,
          what="the relayed group message to reach the protocol端")

    # 4) Enter a TRPG session: group messages are withheld and sends are rejected...
    upstream.group_message(".log new relaytest")
    upstream.wait_for("send_group_msg", "已新开记录日志", timeout=30)

    seen = len(ml.group_events(GROUP))
    upstream.group_message("during session")
    time.sleep(2.5)
    assert len(ml.group_events(GROUP)) == seen, "a group message leaked to 麦bot during a TRPG session"

    blocked = ml.request("send_group_msg", {"group_id": GROUP, "message": "should-not-send"})
    assert blocked["status"] == "failed" and blocked["retcode"] != 0, blocked
    assert upstream.find("send_group_msg", "should-not-send") == [], "blocked message reached the protocol端"
    # ...while queries and other actions still work in-session.
    assert ml.request("get_group_list")["data"]

    # 5) Leave the session: relaying resumes in both directions.
    upstream.group_message(".log off")
    upstream.wait_for("send_group_msg", "已暂停日志", timeout=30)
    seen = len(ml.group_events(GROUP))
    deadline = time.monotonic() + 24
    while time.monotonic() < deadline and len(ml.group_events(GROUP)) == seen:
        upstream.group_message("after session")
        time.sleep(2.0)
    assert len(ml.group_events(GROUP)) > seen, "relaying did not resume after .log off"
    assert ml.request("send_group_msg", {"group_id": GROUP, "message": "ok-again"})["status"] != "failed"
