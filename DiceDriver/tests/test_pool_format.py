# -*- encoding: utf-8 -*-
"""
@File     :   test_pool_format.py
@Desc     :   pool_format.py 的性质测试：往返一致、分片长度、单字符改动被发现、信封边界
@Note     :   DiceSrc 没有 C++ 测试骨架，RandomGenerator 的编解码又位于匿名命名空间、外部调不到，
              所以格式先在这里落成一份独立实现，Task 2 的 e2e 再拿它去交叉验证 C++ 侧
"""

import json

import pool_format
from pool_format import CHUNK, decode_pool, encode_pool

WORDS = [(i * 7919) % 65536 for i in range(1000)]


def test_round_trip_preserves_words_and_bit_buffer():
    assert decode_pool(encode_pool(WORDS, 0x1FF, 9)) == (WORDS, 0x1FF, 9)


def test_envelope_is_multiline_and_chunks_are_at_most_76_chars():
    text = encode_pool(WORDS, 0, 0)
    doc = json.loads(text)
    assert doc["version"] == 2 and doc["bitBuf"] == 0 and doc["bitCnt"] == 0
    assert len(text.splitlines()) >= 10
    assert all(isinstance(c, str) and 0 < len(c) <= CHUNK for c in doc["words"])
    assert len(doc["words"]) > 1


def test_single_character_edit_is_detected():
    text = encode_pool(WORDS, 0, 0)
    doc = json.loads(text)
    doc["words"][0] = ("B" if doc["words"][0][0] != "B" else "C") + doc["words"][0][1:]
    assert decode_pool(json.dumps(doc)) is None


def test_structurally_invalid_input_returns_none():
    for text in ("", "not json", json.dumps({"version": 1, "bitBuf": 0, "bitCnt": 0, "words": []}),
                 json.dumps({"version": 2, "bitBuf": 0, "bitCnt": 0, "words": [123]}),
                 json.dumps({"version": 2, "bitBuf": 0, "bitCnt": 0, "words": ["AAAA"]})):
        assert decode_pool(text) is None, text


def test_bit_buffer_validation():
    good = json.loads(encode_pool(WORDS, 0x1FF, 9))
    for bit_cnt, bit_buf in ((16, 0), (-1, 0), (9, 0x200), (0, 1)):
        doc = dict(good, bitCnt=bit_cnt, bitBuf=bit_buf)
        assert decode_pool(json.dumps(doc)) is None, (bit_cnt, bit_buf)


def test_trailing_junk_is_rejected():
    # 尾部塞上字母表外的字符：解出来仍是完整正确的内容，靠校验和拦不住，只能靠"能不能原样编码回去"
    doc = json.loads(encode_pool(WORDS, 0, 0))
    doc["words"][-1] += "!!!!"
    assert decode_pool(json.dumps(doc)) is None
