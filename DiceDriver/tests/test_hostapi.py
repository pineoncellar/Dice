"""HostApi behaviour against a fake OneBot bot (no DLL involved)."""

import logging
import time
from pathlib import Path

import pytest

from dicedriver import config
from dicedriver.hostapi import DROPPED, HostApi
from dicedriver.state import State


class FakeBot:
    def __init__(self):
        self.sync: dict = {}  # action -> value | callable | Exception
        self.calls: list[tuple[str, dict]] = []
        self.nowait: list[tuple[str, dict]] = []

    def call_sync(self, action, params=None, timeout=None):
        self.calls.append((action, params or {}))
        v = self.sync.get(action)
        if isinstance(v, Exception):
            raise v
        return v(params) if callable(v) else v

    def call_nowait(self, action, params, what="", timeout=None):
        self.nowait.append((action, params))


class Life:
    self_id = 100
    root_dir = Path("D:/x")

    def __init__(self):
        self.events = []

    def restart(self, reason):
        self.events.append(("restart", reason))

    def exit(self):
        self.events.append(("exit",))


class Sink:
    def __init__(self):
        self.items = []

    def push(self, t):
        self.items.append(t)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = config.from_dict({}, tmp_path)
    bot = FakeBot()
    clock = Clock()
    st = State(bot, cfg.cache, tmp_path / "state", clock)
    life, notif, hb = Life(), Sink(), Sink()
    api = HostApi(cfg, bot, st, notif, hb, life)
    bot.sync["get_group_member_list"] = [
        {"user_id": 100, "role": "admin", "card": "骰娘", "nickname": "bot", "last_sent_time": 50},
        {"user_id": 1, "role": "owner", "card": "", "nickname": "boss", "last_sent_time": 0},
        {"user_id": 2, "role": "member", "card": "c2", "nickname": "n2", "last_sent_time": 0},
    ]
    return api, bot, st, life, notif, hb, clock, cfg


def call(api, name, *ints, text=""):
    return api.dispatch(name, ints, text.encode())


def test_dropped_apis_have_no_handler(env):
    api = env[0]
    names = set(api.names())
    assert not (names & DROPPED)
    assert len(names) == 39
    assert {"DiceUpdate", "GetTinyID", "SetDiscussLeave"} <= names


def test_driver_ver_and_tinyid_and_update(env):
    api = env[0]
    assert b"OneBot11" in call(api, "_DriverVer").out
    assert call(api, "GetTinyID", 100).ret == 0
    r = call(api, "DiceUpdate", text="2.7.1")
    assert r.ret == 1 and r.out


def test_auth_levels_and_roles(env):
    api = env[0]
    assert call(api, "GetGroupAuth", 100, 9, 1).ret == 3
    assert call(api, "GetGroupAuth", 100, 9, 100).ret == 2
    assert call(api, "GetGroupAuth", 100, 9, 2).ret == 1
    assert call(api, "GetGroupAuth", 100, 9, 77).ret == 0  # definitely not a member
    assert call(api, "IsGroupAdmin", 100, 9, 100, 0).ret == 1
    assert call(api, "IsGroupOwner", 100, 9, 100, 1).ret == 0
    assert call(api, "IsGroupMember", 100, 9, 77, 1).ret == 0
    assert call(api, "GetGroupAdminList", 100, 9).out == b"1,100"
    assert call(api, "GetGroupMemberList", 100, 9).out == b"1,2,100"


def test_member_cache_ttl_and_failure_keeps_last(env):
    api, bot, st, _, _, _, clock, _ = env
    call(api, "GetGroupMemberList", 100, 9)
    call(api, "GetGroupMemberList", 100, 9)
    assert sum(1 for a, _ in bot.calls if a == "get_group_member_list") == 1
    clock.t += 61
    bot.sync["get_group_member_list"] = RuntimeError("boom")
    assert call(api, "GetGroupMemberList", 100, 9).out == b"1,2,100"  # stale value kept
    assert sum(1 for a, _ in bot.calls if a == "get_group_member_list") == 2


def test_unknown_membership_falls_back_to_default(env):
    api, bot, *_ = env
    bot.sync["get_group_member_list"] = RuntimeError("down")
    bot.sync["get_group_member_info"] = RuntimeError("down")
    assert call(api, "IsGroupAdmin", 100, 9, 5, 1).ret == 1
    assert call(api, "IsGroupAdmin", 100, 9, 5, 0).ret == 0
    assert call(api, "GetGroupAuth", 100, 9, 5).ret == 0
    assert call(api, "GetGroupMemberList", 100, 9).out == b""


def test_group_nick_size_name_and_printinfo(env):
    api, bot, *_ = env
    bot.sync["get_group_list"] = [{"group_id": 9, "group_name": "测试群", "member_count": 3, "max_member_count": 200}]
    assert call(api, "GetGroupIDList", 100).out == b"9"
    assert call(api, "GetGroupName", 100, 9).out.decode() == "测试群"
    size = call(api, "GetGroupSize", 100, 9).ret
    assert (size >> 32, size & 0xFFFFFFFF) == (3, 200)
    assert call(api, "PrintGroupInfo", 100, 9).out.decode() == "[测试群](9)[3/200]"
    assert call(api, "GetGroupNick", 100, 9, 100).out.decode() == "骰娘"
    assert call(api, "GetGroupNick", 100, 9, 1).out.decode() == "boss"
    assert call(api, "GetGroupNick", 100, 9, 55).out.decode() == "群员"


def test_group_size_unknown_is_zero(env):
    api, bot, *_ = env
    bot.sync["get_group_list"] = []
    bot.sync["get_group_info"] = RuntimeError("x")
    assert call(api, "GetGroupSize", 100, 5).ret == 0


def test_last_speak(env):
    api, _, st, *_ = env
    assert call(api, "GetGroupLastMsg", 100, 9, 2).ret == -1
    st.note_speak(9, 2, 777)
    assert call(api, "GetGroupLastMsg", 100, 9, 2).ret == 777
    call(api, "GetGroupMemberList", 100, 9)  # loads last_sent_time=50 for user 100
    assert call(api, "GetGroupLastMsg", 100, 9, 100).ret == 50


def test_last_speak_persists(env, tmp_path):
    api, bot, st, _, _, _, clock, cfg = env
    st.note_speak(9, 2, 777)
    st.flush()
    again = State(bot, cfg.cache, tmp_path / "state", clock)
    assert again.last_speak(9, 2) == 777


def test_friends_nick_cache(env):
    api, bot, st, *_ = env
    bot.sync["get_friend_list"] = [{"user_id": 5, "nickname": "n", "remark": "r"}]
    assert call(api, "IsFriend", 100, 5, 0).ret == 1
    assert call(api, "IsFriend", 100, 6, 1).ret == 0
    assert call(api, "GetFriendQQList", 100).out == b"5"
    bot.sync["get_stranger_info"] = {"nickname": "陌生人"}
    assert call(api, "GetNick", 100, 8).out.decode() == "陌生人"
    call(api, "GetNick", 100, 8)
    assert sum(1 for a, _ in bot.calls if a == "get_stranger_info") == 1
    bot.sync["get_login_info"] = {"nickname": "self"}
    assert call(api, "GetNick", 100, 100).out == b"self"


def test_friends_unavailable_uses_default(env):
    api, bot, *_ = env
    bot.sync["get_friend_list"] = RuntimeError("down")
    assert call(api, "IsFriend", 100, 5, 1).ret == 1
    assert call(api, "IsFriend", 100, 5, 0).ret == 0


def test_send_converts_cq_to_segments(env):
    api, bot, *_ = env
    call(api, "SendGroupMsg", 100, 9, text="hi[CQ:at,qq=1]")
    action, params = bot.nowait[-1]
    assert action == "send_group_msg" and params["group_id"] == 9
    assert params["message"][1] == {"type": "at", "data": {"qq": "1"}}
    call(api, "SendPrivateMsg", 100, 5, text="")
    assert len(bot.nowait) == 1  # empty messages are not sent


def test_send_string_format(env):
    api, bot, *_ = env
    api.cfg.onebot.send_format = "string"
    call(api, "SendPrivateMsg", 100, 5, text="a[CQ:at,qq=1]")
    assert bot.nowait[-1][1]["message"] == "a[CQ:at,qq=1]"


def test_answer_group_invite_flow(env):
    api, bot, st, *_ = env
    st.put_group_flag(9, "F1", "invite")
    call(api, "AnswerGroupInvited", 100, 9, 1)
    assert bot.nowait[-1] == ("set_group_add_request", {"flag": "F1", "sub_type": "invite", "approve": True, "reason": ""})
    st.put_group_flag(9, "F2", "invite")
    call(api, "AnswerGroupInvited", 100, 9, 3)  # ignore -> reject
    assert bot.nowait[-1][1]["approve"] is False
    n = len(bot.nowait)
    call(api, "AnswerGroupInvited", 100, 9, 1)  # flag gone -> dropped, no action
    assert len(bot.nowait) == n


def test_answer_friend_request(env):
    api, bot, st, *_ = env
    st.put_friend_flag(5, "FF")
    call(api, "AnswerFriendRequest", 100, 5, 2, text="")
    assert bot.nowait[-1] == ("set_friend_add_request", {"flag": "FF", "approve": False, "remark": ""})
    n = len(bot.nowait)
    call(api, "AnswerFriendRequest", 100, 5, 1)
    assert len(bot.nowait) == n


def test_flags_expire(env):
    api, bot, st, _, _, _, clock, cfg = env
    st.put_group_flag(9, "F", "invite")
    clock.t += cfg.cache.flag_ttl_s + 1
    st.tick()
    assert st.pop_group_flag(9) is None


def test_whole_ban_persisted_and_lifted(env, tmp_path):
    api, bot, st, _, _, _, clock, cfg = env
    call(api, "SetGroupWholeBan", 100, 9, 30)
    assert bot.nowait[-1] == ("set_group_whole_ban", {"group_id": 9, "enable": True})
    # a restarted driver reloads the expiry from disk and lifts it when due
    again = State(bot, cfg.cache, tmp_path / "state", clock)
    clock.t += 31
    bot.sync["set_group_whole_ban"] = None
    again.tick()
    assert ("set_group_whole_ban", {"group_id": 9, "enable": False}) in bot.calls
    assert again.due_whole_bans() == []


def test_whole_ban_zero_lifts(env):
    api, bot, st, *_ = env
    call(api, "SetGroupWholeBan", 100, 9, 30)
    call(api, "SetGroupWholeBan", 100, 9, 0)
    assert bot.nowait[-1] == ("set_group_whole_ban", {"group_id": 9, "enable": False})
    assert st.due_whole_bans() == []


def test_admin_actions_map_one_to_one(env):
    api, bot, *_ = env
    call(api, "SetGroupKick", 100, 9, 2)
    call(api, "SetGroupBan", 100, 9, 2, 60)
    call(api, "SetGroupAdmin", 100, 9, 2, 1)
    call(api, "SetGroupCard", 100, 9, 2, text="新名片")
    call(api, "SetGroupTitle", 100, 9, 2, text="头衔")
    call(api, "SetGroupLeave", 100, 9)
    actions = [a for a, _ in bot.nowait]
    assert actions == ["set_group_kick", "set_group_ban", "set_group_admin", "set_group_card",
                       "set_group_special_title", "set_group_leave"]
    assert bot.nowait[1][1]["duration"] == 60
    assert bot.nowait[3][1]["card"] == "新名片"


def test_discuss_leave_warns_and_noop(env, caplog):
    api, bot, *_ = env
    with caplog.at_level(logging.WARNING, logger="dd.api"):
        call(api, "SetDiscussLeave", 100, 9)
    assert not bot.nowait
    assert any("SetDiscussLeave" in r.message for r in caplog.records)


def test_upload_group_file_failure_returns_true(env, tmp_path):
    api, bot, *_ = env
    assert call(api, "UploadGroupFile", 100, 9, text=str(tmp_path / "missing.txt")).ret == 1
    assert not bot.nowait
    f = tmp_path / "log.txt"
    f.write_text("x")
    assert call(api, "UploadGroupFile", 100, 9, text=str(f)).ret == 1
    assert bot.nowait[-1][0] == "upload_group_file"
    assert bot.nowait[-1][1]["name"] == "log.txt"


def test_send_private_file_reports_truth(env, tmp_path):
    api, bot, *_ = env
    f = tmp_path / "a.txt"
    f.write_text("x")
    bot.sync["upload_private_file"] = None
    assert call(api, "SendPrivateFile", 100, 5, text=str(f)).ret == 1
    bot.sync["upload_private_file"] = RuntimeError("no")
    assert call(api, "SendPrivateFile", 100, 5, text=str(f)).ret == 0
    assert call(api, "SendPrivateFile", 100, 5, text=str(tmp_path / "zz")).ret == 0


def test_lifecycle_calls(env):
    api, _, _, life, notif, hb, *_ = env
    call(api, "Reload", )
    call(api, "Remake")
    call(api, "Killme")
    assert life.events == [("restart", "reload"), ("restart", "remake"), ("exit",)]
    call(api, "DebugMsg", 100, text="hello")
    call(api, "DiceHeartbeat", 100, text="&masterID=1")
    assert notif.items == ["hello"] and hb.items == ["&masterID=1"]


def test_get_root_dir_is_ansi_bytes(env):
    api = env[0]
    assert call(api, "GetRootDir").out == b"D:\\x" or call(api, "GetRootDir").out.endswith(b"x")


def test_handler_exception_returns_fallback(env):
    api, bot, st, *_ = env
    api.state = None  # make handlers explode
    assert call(api, "IsGroupAdmin", 100, 9, 1, 1).ret == 1
    assert call(api, "IsGroupAdmin", 100, 9, 1, 0).ret == 0
    assert call(api, "GetGroupLastMsg", 100, 9, 1).ret == -1
    assert call(api, "GetGroupAuth", 100, 9, 1).ret == 0
