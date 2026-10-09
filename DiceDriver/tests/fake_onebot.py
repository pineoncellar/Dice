"""A scripted OneBot 11 implementation (forward WS server) for end-to-end tests."""

from __future__ import annotations

import asyncio
import json
import threading
import time

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

BOT_QQ = 10000
MASTER_QQ = 10001
GROUP = 20001


class FakeOneBot:
    def __init__(self, reverse_url: str = ""):
        self.reverse_url = reverse_url  # set => act as an OneBot client that dials the driver
        self.actions: list[tuple[str, dict]] = []
        self.port = 0
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._ws = None
        self._ready = threading.Event()
        self.connected = threading.Event()
        self._msg_id = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ------------------------------------------------------------------ server

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(10), "fake OneBot did not start"

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        if self.reverse_url:
            self._ready.set()
            while True:
                try:
                    async with connect(self.reverse_url, additional_headers={"X-Self-ID": str(BOT_QQ)},
                                       max_size=None) as ws:
                        await self._handler(ws)
                except Exception:
                    await asyncio.sleep(0.3)
        async with serve(self._handler, "127.0.0.1", 0, max_size=None) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await asyncio.Future()

    def _data_for(self, action: str, params: dict):
        if action == "get_login_info":
            return {"user_id": BOT_QQ, "nickname": "测试骰娘"}
        if action == "get_group_list":
            return [{"group_id": GROUP, "group_name": "测试群", "member_count": 3, "max_member_count": 500}]
        if action == "get_friend_list":
            return [{"user_id": MASTER_QQ, "nickname": "主人", "remark": ""}]
        if action == "get_group_member_list":
            return [
                {"user_id": BOT_QQ, "role": "admin", "card": "", "nickname": "测试骰娘", "last_sent_time": 0},
                {"user_id": MASTER_QQ, "role": "owner", "card": "", "nickname": "主人", "last_sent_time": 0},
                {"user_id": 10002, "role": "member", "card": "路人", "nickname": "路人甲", "last_sent_time": 0},
            ]
        if action == "get_stranger_info":
            return {"user_id": params.get("user_id"), "nickname": f"用户{params.get('user_id')}"}
        if action.startswith("send_"):
            self._msg_id += 1
            return {"message_id": self._msg_id}
        return {}

    async def _handler(self, ws) -> None:
        self._ws = ws
        self.connected.set()
        async for raw in ws:
            req = json.loads(raw)
            action, params = req["action"], req.get("params", {})
            with self._lock:
                self.actions.append((action, params))
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": self._data_for(action, params),
                                      "echo": req.get("echo")}, ensure_ascii=False))

    # ------------------------------------------------------------------ test helpers

    def push(self, event: dict) -> None:
        event.setdefault("self_id", BOT_QQ)
        event.setdefault("time", int(time.time()))
        fut = asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(event, ensure_ascii=False)), self._loop)
        fut.result(5)

    def group_message(self, text: str, uid: int = MASTER_QQ, gid: int = GROUP, as_array: bool = True) -> None:
        message = [{"type": "text", "data": {"text": text}}] if as_array else text
        self._msg_id += 1
        self.push({"post_type": "message", "message_type": "group", "sub_type": "normal", "group_id": gid,
                   "user_id": uid, "message_id": 1000 + self._msg_id, "message": message, "raw_message": text})

    def private_message(self, text: str, uid: int = MASTER_QQ) -> None:
        self._msg_id += 1
        self.push({"post_type": "message", "message_type": "private", "sub_type": "friend", "user_id": uid,
                   "message_id": 2000 + self._msg_id,
                   "message": [{"type": "text", "data": {"text": text}}], "raw_message": text})

    def find(self, action: str, contains: str = "") -> list[dict]:
        with self._lock:
            hits = [p for a, p in self.actions if a == action]
        if contains:
            hits = [p for p in hits if contains in json.dumps(p, ensure_ascii=False)]
        return hits

    def wait_for(self, action: str, contains: str = "", timeout: float = 15.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hits = self.find(action, contains)
            if hits:
                return hits[-1]
            time.sleep(0.1)
        raise AssertionError(f"no {action} containing {contains!r} within {timeout}s; "
                             f"seen: {[a for a, _ in self.actions][-15:]}")
