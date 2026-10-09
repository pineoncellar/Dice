"""CQ-code <-> OneBot 11 message segment conversion."""

from __future__ import annotations

import re
from typing import Any

_CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_.\-]+)((?:,[^,\[\]=]+=[^,\[\]]*)*)\]")


def escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def escape_param(s: str) -> str:
    return escape_text(s).replace(",", "&#44;")


def unescape(s: str) -> str:
    return s.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def segments_to_cq(segments: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for seg in segments:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            out.append(escape_text(str(data.get("text", ""))))
        else:
            params = "".join(f",{k}={escape_param(str(v))}" for k, v in data.items())
            out.append(f"[CQ:{kind}{params}]")
    return "".join(out)


def cq_to_segments(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    pos = 0

    def add_text(raw: str) -> None:
        if raw:
            segments.append({"type": "text", "data": {"text": unescape(raw)}})

    for m in _CQ_RE.finditer(text):
        add_text(text[pos:m.start()])
        data: dict[str, str] = {}
        for pair in m.group(2).split(",")[1:]:
            key, _, value = pair.partition("=")
            data[key] = unescape(value)
        segments.append({"type": m.group(1), "data": data})
        pos = m.end()
    add_text(text[pos:])
    return segments


def message_to_cq(message: Any, raw_message: str | None = None) -> str:
    """Normalise whatever an OneBot implementation delivers into the CQ string Dice expects."""
    if isinstance(message, list):
        return segments_to_cq(message)
    if isinstance(message, str):
        return message
    return raw_message or ""
