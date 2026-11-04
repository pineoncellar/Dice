"""ctypes bridge: loads dd_shim.dll (host API thunks) and the Dice DLL (event exports).

Every call into the Dice DLL - events and lifecycle alike - runs on one dedicated thread. Dice
writes global state without locks (e.g. `ChatList` in ManagerSystem.cpp), so two threads inside
`eventGroupMsg` at once corrupts it: that is the access violation and the wedge this project hit.
The host API direction stays as it is - Dice calls us from whichever of its own threads it likes.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import c_char_p, c_int, c_longlong, c_void_p
from pathlib import Path

from . import crashguard, logs
from .hostapi import DROPPED, HostApi

log = logging.getLogger("dd.native")

_DISPATCH_T = ctypes.CFUNCTYPE(c_longlong, c_int, ctypes.POINTER(c_longlong), c_int, c_char_p, c_void_p, c_int)

# name -> (argtypes) ; every Dice export returns int except the void lifecycle ones
_EVENTS: dict[str, tuple[type, ...]] = {
    "eventPrivateMsg": (c_int, c_longlong, c_char_p),
    "eventGroupMsg": (c_int, c_longlong, c_longlong, c_char_p),
    "eventGroupMemberAdd": (c_longlong, c_longlong, c_longlong),
    "eventGroupMemberKicked": (c_longlong, c_longlong, c_longlong),
    "eventGroupBan": (c_longlong, c_longlong, c_longlong, c_char_p),
    "eventGroupInvited": (c_longlong, c_longlong),
    "eventFriendRequest": (c_longlong, c_char_p),
    "eventFriendAdd": (c_longlong,),
}


class NativeError(RuntimeError):
    pass


def clamp_int32(v: int) -> int:
    return ctypes.c_int32(v).value


class DiceNative:
    # A single call taking longer than this gets logged while it is still running, so a wedged
    # DLL shows up in the journal instead of only as a bot that stopped answering.
    STUCK_WARN_S = 30.0

    def __init__(self, shim_path: Path, dice_dll_path: Path, api: HostApi):
        self.shim_path = shim_path
        self.dice_dll_path = dice_dll_path
        self.api = api
        self._shim = None
        self._dice = None
        self._names: list[str] = []
        self._callback = None  # must stay referenced for the lifetime of the process
        self._events: dict[str, object] = {}
        self._calls: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._thread_ident = 0
        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------------ loading

    def load(self) -> None:
        for p in (self.shim_path, self.dice_dll_path):
            if not p.is_file():
                raise NativeError(f"file not found: {p}")
        shim = ctypes.CDLL(str(self.shim_path))
        shim.dd_api_count.restype = c_int
        shim.dd_api_name.restype = c_char_p
        shim.dd_api_name.argtypes = [c_int]
        shim.dd_api_list_getter.restype = c_void_p
        shim.dd_set_dispatch.argtypes = [_DISPATCH_T]
        shim.dd_set_dispatch.restype = None
        shim.dd_out_capacity.restype = c_int
        self._shim = shim
        self._out_cap = shim.dd_out_capacity()

        self._names = [shim.dd_api_name(i).decode() for i in range(shim.dd_api_count())]
        handlers = set(self.api.names())
        missing = [n for n in self._names if n not in handlers]
        if missing:
            raise NativeError(f"shim exports APIs without Python handlers: {missing}")
        leaked = [n for n in self._names if n in DROPPED]
        if leaked:
            raise NativeError(f"shim registers APIs that are meant to be dropped: {leaked}")
        log.info("shim loaded: %d host APIs registered (dropped: %s)", len(self._names), ", ".join(sorted(DROPPED)))

        self._callback = _DISPATCH_T(self._dispatch)
        shim.dd_set_dispatch(self._callback)

        log.info("loading Dice DLL %s", self.dice_dll_path)
        dice = ctypes.CDLL(str(self.dice_dll_path))
        for name in ("eventStartUp", "eventEnable", "eventDisable", "eventExit"):
            if not hasattr(dice, name):
                raise NativeError(f"Dice DLL lacks required export {name}")
        dice.eventStartUp.argtypes = [c_void_p, c_longlong]
        dice.eventStartUp.restype = None
        for name in ("eventEnable", "eventDisable", "eventExit"):
            fn = getattr(dice, name)
            fn.argtypes = []
            fn.restype = None
        for name, argtypes in _EVENTS.items():
            fn = getattr(dice, name, None)
            if fn is None:
                log.warning("Dice DLL lacks export %s; that event type will be ignored", name)
                continue
            fn.argtypes = list(argtypes)
            fn.restype = c_int
            self._events[name] = fn
        self._dice = dice

    # ------------------------------------------------------------------ the one thread allowed in

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._thread_lock:
            if self._thread is None:
                thread = threading.Thread(target=self._run_calls, name="dd-dice", daemon=True)
                thread.start()
                self._thread = thread

    def _run_calls(self) -> None:
        self._thread_ident = threading.get_ident()
        while True:
            item = self._calls.get()
            if item is None:
                return
            fn, box, done = item
            try:
                box.append(("ok", fn()))
            except BaseException as e:
                box.append(("err", e))
            finally:
                done.set()
                crashguard.tick("dice")

    def _call(self, what: str, fn):
        """Run fn on the Dice thread and hand its outcome back to the caller.

        Calls are strictly serialised in submission order, which is also what keeps Dice's
        per-chat handling in order. A wedged call cannot be cancelled: it is reported every
        STUCK_WARN_S seconds and the caller keeps waiting, so nothing else enters the DLL.
        """
        if threading.get_ident() == self._thread_ident:  # already inside: never queue behind self
            return fn()
        self._ensure_thread()
        box: list = []
        done = threading.Event()
        self._calls.put((fn, box, done))
        started = time.monotonic()
        while not done.wait(self.STUCK_WARN_S):
            log.error("%s has been running on the Dice thread for %.0fs; the DLL looks wedged",
                      what, time.monotonic() - started)
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    # ------------------------------------------------------------------ host API callback

    def _dispatch(self, api_id, ints_ptr, n, text, out, cap):
        try:
            name = self._names[api_id]
            crashguard.breadcrumb(f"api {name}")
            ints = tuple(ints_ptr[i] for i in range(n))
            result = self.api.dispatch(name, ints, text or b"")
            data = result.out
            if len(data) >= cap:
                log.warning("%s result of %d bytes truncated to %d", name, len(data), cap - 1)
                data = data[: cap - 1]
                if name.startswith("Get") and b"," in data:  # id lists: cut at a separator
                    data = data[: data.rfind(b",")]
            ctypes.memmove(out, data + b"\0", len(data) + 1)
            return result.ret
        except BaseException:  # a Python exception must never unwind into Dice
            log.exception("dispatch crashed (api id %s)", api_id)
            try:
                ctypes.memset(out, 0, 1)
            except Exception:
                pass
            return 0

    # ------------------------------------------------------------------ lifecycle

    def start(self, bot_qq: int) -> None:
        def run() -> None:
            getter = self._shim.dd_api_list_getter()
            self._dice.eventStartUp(getter, bot_qq)
            self._dice.eventEnable()

        self._call("eventStartUp", run)

    def disable(self) -> None:
        self._call("eventDisable", self._dice.eventDisable)

    def exit(self) -> None:
        self._call("eventExit", self._dice.eventExit)

    # ------------------------------------------------------------------ events

    def emit(self, name: str, *args) -> int:
        fn = self._events.get(name)
        if fn is None:
            return 0
        enc = [a.encode("utf-8") if isinstance(a, str) else a for a in args]
        crashguard.breadcrumb(f"event {name}")
        logs.trace(log, "event> %s%s", name, args)
        ret = self._call(f"event {name}", lambda: fn(*enc))
        logs.trace(log, "event< %s -> %s", name, ret)
        return ret
