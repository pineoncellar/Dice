import pytest

from dicedriver import config


def test_defaults_validate():
    cfg = config.from_dict({}, config.Path("."))
    assert cfg.onebot.mode == "forward"


def test_unknown_key_rejected():
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.from_dict({"onebot": {"urll": "x"}}, config.Path("."))


def test_unknown_section_rejected():
    with pytest.raises(config.ConfigError, match="unknown section"):
        config.from_dict({"nope": {}}, config.Path("."))


def test_type_errors():
    with pytest.raises(config.ConfigError):
        config.from_dict({"onebot": {"port": "6700"}}, config.Path("."))
    with pytest.raises(config.ConfigError):
        config.from_dict({"log": {"console": "yes"}}, config.Path("."))


def test_notify_private_needs_targets():
    with pytest.raises(config.ConfigError, match="target"):
        config.from_dict({"notify": {"mode": "private"}}, config.Path("."))
    cfg = config.from_dict({"notify": {"mode": "group", "targets": [123]}}, config.Path("."))
    assert cfg.notify.targets == [123]


def test_mode_and_format_validation():
    with pytest.raises(config.ConfigError):
        config.from_dict({"onebot": {"mode": "sideways"}}, config.Path("."))
    with pytest.raises(config.ConfigError):
        config.from_dict({"onebot": {"mode": "forward", "url": "http://x"}}, config.Path("."))


def test_bridge_defaults_and_validation():
    cfg = config.from_dict({}, config.Path("."))
    assert cfg.bridge.enabled is False and cfg.bridge.port == 6701
    with pytest.raises(config.ConfigError, match="on_unknown"):
        config.from_dict({"bridge": {"on_unknown": "maybe"}}, config.Path("."))
    with pytest.raises(config.ConfigError, match="port"):
        config.from_dict({"bridge": {"port": 0}}, config.Path("."))
    with pytest.raises(config.ConfigError, match="path"):
        config.from_dict({"bridge": {"path": "no-slash"}}, config.Path("."))
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.from_dict({"bridge": {"enable": True}}, config.Path("."))


def test_bridge_must_not_collide_with_the_reverse_listener():
    reverse = {"mode": "reverse", "host": "127.0.0.1", "port": 6700, "path": "/"}
    with pytest.raises(config.ConfigError, match="collides"):
        config.from_dict({"onebot": reverse, "bridge": {"enabled": True, "host": "127.0.0.1", "port": 6700}},
                         config.Path("."))
    # same port is fine when the upstream is a client, not a listener
    config.from_dict({"onebot": {"mode": "forward"}, "bridge": {"enabled": True, "port": 6700}},
                     config.Path("."))


def test_example_config_loads(tmp_path):
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "dicedriver.example.toml"
    cfg = config.load(example)
    workspace = example.parent.parent
    assert cfg.resolve(cfg.driver.shim_dll) == workspace / "output" / "dd_shim.dll"
    assert cfg.resolve(cfg.driver.dice_dll).parent == workspace / "output"
    assert cfg.resolve(cfg.driver.root_dir) == workspace / "data"
    assert cfg.resolve("x").parent == example.parent
