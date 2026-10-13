# -*- encoding: utf-8 -*-
"""
@File     :   pool_format.py
@Desc     :   random_pool.json 的编解码，与 DiceSrc/Dice/RandomGenerator.cpp 的 v2 文件格式逐字节对齐
@Note     :   KEY 必须和 C++ 侧的 kPoolKey 一字不差。任何一侧单方面改格式，另一侧都会把整池当垃圾丢掉，
              而且是静默丢掉——所以这里的每个校验都直接对应 C++ readPool() 里的一条判断
"""

from __future__ import annotations

import base64
import json

# 与 RandomGenerator.cpp 的 kPoolKey 一字不差
KEY = bytes((
    0x7E, 0xD3, 0x1A, 0x94, 0x2F, 0xB8, 0x51, 0xC6,
    0x0D, 0xA2, 0x63, 0xF0, 0x39, 0x8E, 0x15, 0xCC,
    0x47, 0xBA, 0x03, 0x96, 0x2B, 0xD8, 0x6F, 0xE4,
    0x1D, 0x82, 0x5B, 0xA0, 0x37, 0xFE, 0x69, 0xC4,
))
CHUNK = 76
VERSION = 2
WORD_BITS = 16


# --------------------------------------------------------------------- 原语

def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def _xor(data: bytes) -> bytes:
    # XOR 是自己的逆运算，加解密共用这一个函数
    return bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(data))


def _pack(words) -> bytes:
    out = bytearray()
    for w in words:
        out += int(w).to_bytes(2, "little")
    return bytes(out)


def _unpack(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


# ----------------------------------------------------------------- 信封读写

def encode_pool(words, bit_buf: int, bit_cnt: int) -> str:
    cipher = _xor(_pack(words))
    blob = fnv1a32(cipher).to_bytes(4, "big") + cipher
    text = base64.b64encode(blob).decode("ascii")
    # 键序按字典序排，和 nlohmann 的默认输出一致，两边写出来的文件长得一样，对不上时一眼能看出来
    doc = {"bitBuf": int(bit_buf), "bitCnt": int(bit_cnt), "version": VERSION,
           "words": [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]}
    return json.dumps(doc, indent=4, ensure_ascii=False)


# [读回池文件]
#         None                    (words, bitBuf, bitCnt)
# 信封 / 位数 / 长度 / 校验和任一处不可信    全部校验通过后才解到明文
def decode_pool(text: str):
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        return None                                   # 状态：信封不认，旧 v1 与手改过的文件都从这里退出
    bit_cnt, bit_buf, chunks = doc.get("bitCnt"), doc.get("bitBuf"), doc.get("words")
    if not isinstance(bit_cnt, int) or not isinstance(bit_buf, int):
        return None
    if not isinstance(chunks, list) or not all(isinstance(c, str) for c in chunks):
        return None
    # 取用后零散位总是少于一个整字，且装不下这么多位
    if not (0 <= bit_cnt < WORD_BITS) or not (0 <= bit_buf < (1 << bit_cnt)):
        return None
    try:
        blob = base64.b64decode("".join(chunks), validate=True)
    except ValueError:
        return None                                   # 状态：字母表外的东西，base64 直接拒收
    if len(blob) < 6 or (len(blob) - 4) % 2:
        return None
    if int.from_bytes(blob[:4], "big") != fnv1a32(blob[4:]):
        return None                                   # 状态：校验和不符，当作没这个池，宁可重新取数
    return _unpack(_xor(blob[4:])), bit_buf, bit_cnt
