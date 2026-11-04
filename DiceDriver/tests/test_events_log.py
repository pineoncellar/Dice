"""消息日志的边界：哪些内容会进日志、哪些不会，以及结果有没有被记下来。"""

from types import SimpleNamespace

import pytest

from dicedriver.events import EventRouter, _clip, command_of


class FakeNative:
    """Records what Dice was asked to do; `api.sends` mimics the reply counter."""

    def __init__(self, sends: int = 0):
        self.api = SimpleNamespace(sends=0)
        self.calls: list[tuple] = []
        self.replies = sends

    def emit(self, name, *args):
        self.calls.append((name,) + args)
        self.api.sends += self.replies
        return 0


def _router(native):
    cfg = SimpleNamespace(onebot=SimpleNamespace(forward_self_messages=False))
    return EventRouter(cfg, SimpleNamespace(group_ids=lambda: {1}, group_added=lambda gid: None,
                                            note_speak=lambda *a: None),
                       native, lambda: 100, None, lambda: True)


@pytest.mark.parametrize("text", [".pc show", "。pc show", "！r", "．sn", "!help", "  .sn 卡名"])
def test_command_prefixes_follow_dice_strip(text):
    assert command_of(text) is not None


@pytest.mark.parametrize("text", ["你好", "1", "", "   ", "骰子 .pc", "【.pc】"])
def test_plain_text_is_not_a_command(text):
    assert command_of(text) is None


def test_plain_text_content_never_reaches_the_log(caplog):
    from dicedriver.logs import TRACE

    caplog.set_level(TRACE, logger="dd.events")
    router = _router(FakeNative())
    router._message({"post_type": "message", "message_type": "group", "group_id": 1,
                     "user_id": 200, "message_id": 5, "raw_message": "这句聊天记录不该进日志",
                     "message": [{"type": "text", "data": {"text": "这句聊天记录不该进日志"}}]})
    assert "这句聊天记录不该进日志" not in caplog.text
    assert "非指令" in caplog.text and "len=11" in caplog.text


def test_plain_text_is_invisible_at_the_default_debug_level(caplog):
    caplog.set_level("DEBUG", logger="dd.events")
    router = _router(FakeNative())
    router._message({"post_type": "message", "message_type": "group", "group_id": 1,
                     "user_id": 200, "message_id": 5, "raw_message": "嗯",
                     "message": [{"type": "text", "data": {"text": "嗯"}}]})
    assert caplog.text == ""


def test_command_text_and_result_are_logged(caplog):
    caplog.set_level("DEBUG", logger="dd.events")
    native = FakeNative(sends=1)
    router = _router(native)
    router._message({"post_type": "message", "message_type": "group", "group_id": 1,
                     "user_id": 200, "message_id": 6, "raw_message": ".sn 一条林",
                     "message": [{"type": "text", "data": {"text": ".sn 一条林"}}]})
    assert "'.sn 一条林'" in caplog.text
    assert "回复 1 条" in caplog.text and "ret=0" in caplog.text
    assert native.calls == [("eventGroupMsg", 6, 1, 200, ".sn 一条林")]


def test_private_message_uses_the_private_event(caplog):
    caplog.set_level("DEBUG", logger="dd.events")
    native = FakeNative()
    router = _router(native)
    router._message({"post_type": "message", "message_type": "private", "user_id": 200,
                     "message_id": 7, "raw_message": ".r",
                     "message": [{"type": "text", "data": {"text": ".r"}}]})
    assert native.calls == [("eventPrivateMsg", 7, 200, ".r")]
    assert "私聊" in caplog.text


def test_long_commands_are_clipped_but_kept_recognisable():
    shown = _clip("." + "x" * 400)
    assert shown.startswith(".xxx")
    assert shown.endswith("(+101字)")
