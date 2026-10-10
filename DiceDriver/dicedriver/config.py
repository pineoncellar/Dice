"""Configuration loading and validation (dicedriver.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass
class DriverCfg:
    bot_qq: int = 0  # 0 = take it from the OneBot connection
    dice_dll: str = "../output/w4123.Dice.windows.amd64.dll"
    shim_dll: str = "../output/dd_shim.dll"
    root_dir: str = "../data"  # Dice creates <root_dir>/Dice<QQ>/ ; must be ANSI-code-page safe
    state_dir: str = "../data/driver-state"
    pool_size: int = 16  # worker threads that call into Dice
    startup_timeout_s: int = 60


@dataclass
class OneBotCfg:
    mode: str = "forward"  # forward | reverse
    url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    host: str = "127.0.0.1"
    port: int = 6700
    path: str = "/"
    call_timeout_s: float = 10.0
    upload_timeout_s: float = 60.0
    send_format: str = "array"  # array | string
    forward_self_messages: bool = False
    friend_greeting: bool = True  # send Dice's welcome text after accepting a friend request


@dataclass
class BridgeCfg:
    """OneBot relay endpoint for an external bot (麦bot): 我们从上游转出/转入它。

    Disabled by default. When enabled the driver listens on host:port and speaks OneBot 11
    to whoever connects, proxying actions upstream and relaying events downstream.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6701
    path: str = "/"
    access_token: str = ""
    gate_private: bool = False  # also gate private chats bound to a TRPG session
    relay_self_messages: bool = False  # relay message_sent (our own bots' echoes) downstream
    on_unknown: str = "closed"  # closed | open — before the first snapshot completes
    snapshot_grace_s: float = 30.0  # how long to keep that default before giving up on snapshots
    queue_limit: int = 1000  # per-client backlog of relayed events before dropping


@dataclass
class LogCfg:
    file: str = "logs/dicedriver.log"
    dice_log_file: str = "logs/dice-{qq}.log"
    level: str = "debug"  # trace | debug | info | warn | error
    max_size_mb: int = 64
    files: int = 10
    console: bool = True


@dataclass
class NotifyCfg:
    mode: str = "console"  # off | console | private | group
    targets: list[int] = field(default_factory=list)
    batch_window_ms: int = 1000
    rate_limit: int = 20  # per minute


@dataclass
class HeartbeatCfg:
    enabled: bool = False
    url: str = "http://127.0.0.1:8787/heartbeat"
    content_type: str = "application/x-www-form-urlencoded"
    timeout_ms: int = 300
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CacheCfg:
    member_ttl_s: float = 60.0
    nick_ttl_s: float = 600.0
    friend_ttl_s: float = 300.0
    group_refresh_s: float = 600.0
    last_speak_flush_s: float = 60.0
    flag_ttl_s: float = 3 * 86400.0


@dataclass
class Config:
    driver: DriverCfg = field(default_factory=DriverCfg)
    onebot: OneBotCfg = field(default_factory=OneBotCfg)
    bridge: BridgeCfg = field(default_factory=BridgeCfg)
    log: LogCfg = field(default_factory=LogCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)
    heartbeat: HeartbeatCfg = field(default_factory=HeartbeatCfg)
    cache: CacheCfg = field(default_factory=CacheCfg)
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            path = self.base_dir / path
        # normalise ".." lexically (no symlink resolution: configured paths may not exist yet)
        return Path(os.path.normpath(path))


_SECTIONS = {
    "driver": DriverCfg,
    "onebot": OneBotCfg,
    "bridge": BridgeCfg,
    "log": LogCfg,
    "notify": NotifyCfg,
    "heartbeat": HeartbeatCfg,
    "cache": CacheCfg,
}


def _fill(section: str, cls: type, raw: dict[str, Any]):
    obj = cls()
    for key, value in raw.items():
        if not hasattr(obj, key):
            raise ConfigError(f"[{section}] unknown key: {key}")
        default = getattr(obj, key)
        if isinstance(default, bool) and not isinstance(value, bool):
            raise ConfigError(f"[{section}] {key} must be true/false")
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"[{section}] {key} must be a number")
            value = type(default)(value)
        elif isinstance(default, str) and not isinstance(value, str):
            raise ConfigError(f"[{section}] {key} must be a string")
        elif isinstance(default, list) and not isinstance(value, list):
            raise ConfigError(f"[{section}] {key} must be a list")
        elif isinstance(default, dict) and not isinstance(value, dict):
            raise ConfigError(f"[{section}] {key} must be a table")
        setattr(obj, key, value)
    return obj


def validate(cfg: Config) -> None:
    ob = cfg.onebot
    if ob.mode not in ("forward", "reverse"):
        raise ConfigError("[onebot] mode must be forward or reverse")
    if ob.send_format not in ("array", "string"):
        raise ConfigError("[onebot] send_format must be array or string")
    if ob.mode == "forward" and not ob.url.startswith(("ws://", "wss://")):
        raise ConfigError("[onebot] url must start with ws:// or wss://")
    if not (0 < ob.port < 65536):
        raise ConfigError("[onebot] port out of range")
    if not ob.path.startswith("/"):
        raise ConfigError("[onebot] path must start with /")
    if cfg.log.level.lower() not in ("trace", "debug", "info", "warn", "warning", "error"):
        raise ConfigError("[log] level must be trace/debug/info/warn/error")
    if cfg.notify.mode not in ("off", "console", "private", "group"):
        raise ConfigError("[notify] mode must be off/console/private/group")
    if cfg.notify.mode in ("private", "group") and not cfg.notify.targets:
        raise ConfigError(f"[notify] mode={cfg.notify.mode} needs at least one target")
    if not all(isinstance(t, int) and not isinstance(t, bool) for t in cfg.notify.targets):
        raise ConfigError("[notify] targets must be integers")
    if cfg.driver.pool_size < 2:
        raise ConfigError("[driver] pool_size must be >= 2")
    br = cfg.bridge
    if br.on_unknown not in ("closed", "open"):
        raise ConfigError("[bridge] on_unknown must be closed or open")
    if not (0 < br.port < 65536):
        raise ConfigError("[bridge] port out of range")
    if not br.path.startswith("/"):
        raise ConfigError("[bridge] path must start with /")
    if br.queue_limit < 1:
        raise ConfigError("[bridge] queue_limit must be >= 1")
    if (br.enabled and ob.mode == "reverse"
            and br.host == ob.host and br.port == ob.port
            and br.path.rstrip("/") == ob.path.rstrip("/")):
        raise ConfigError("[bridge] host/port/path collides with the Reverse listener in [onebot]")


def from_dict(raw: dict[str, Any], base_dir: Path) -> Config:
    cfg = Config(base_dir=base_dir)
    for name, value in raw.items():
        if name not in _SECTIONS:
            raise ConfigError(f"unknown section: [{name}]")
        if not isinstance(value, dict):
            raise ConfigError(f"[{name}] must be a table")
        setattr(cfg, name, _fill(name, _SECTIONS[name], value))
    validate(cfg)
    return cfg


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (copy dicedriver.example.toml)") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return from_dict(raw, path.parent)
