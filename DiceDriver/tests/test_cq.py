from dicedriver.cq import cq_to_segments, message_to_cq, segments_to_cq


def test_roundtrip_plain_and_cq():
    text = "你好[CQ:at,qq=123]世界[CQ:image,file=file:///C:/a.png]"
    segs = cq_to_segments(text)
    assert [s["type"] for s in segs] == ["text", "at", "text", "image"]
    assert segs[1]["data"] == {"qq": "123"}
    assert segments_to_cq(segs) == text


def test_escapes():
    segs = cq_to_segments("a&amp;b &#91;x&#93;[CQ:face,id=1,name=a&#44;b]")
    assert segs[0] == {"type": "text", "data": {"text": "a&b [x]"}}
    assert segs[1]["data"]["name"] == "a,b"
    assert segments_to_cq(segs) == "a&amp;b &#91;x&#93;[CQ:face,id=1,name=a&#44;b]"


def test_incoming_array_message_becomes_cq():
    msg = [{"type": "text", "data": {"text": ".r d20 [x]"}}, {"type": "at", "data": {"qq": 42}}]
    assert message_to_cq(msg) == ".r d20 &#91;x&#93;[CQ:at,qq=42]"


def test_string_message_passthrough_and_fallback():
    assert message_to_cq("[CQ:at,qq=1] .r") == "[CQ:at,qq=1] .r"
    assert message_to_cq(None, "raw") == "raw"
    assert message_to_cq(None) == ""
