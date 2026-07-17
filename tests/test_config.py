import configparser

import pytest

import fairyfishnet.config as config
from fairyfishnet.errors import ConfigError
from tests.helpers import make_conf


@pytest.mark.parametrize("value", ["y", "yes", "TRUE", "1", "ok"])
def test_parse_bool_true_values(value):
    assert config.parse_bool(value) is True


@pytest.mark.parametrize("value", ["n", "no", "false", "0", "nope"])
def test_parse_bool_false_values(value):
    assert config.parse_bool(value) is False


def test_parse_bool_uses_default_for_empty_input():
    assert config.parse_bool("") is False
    assert config.parse_bool("  ", default=True) is True
    assert config.parse_bool(None, default=True) is True


def test_parse_bool_rejects_unknown_value():
    with pytest.raises(ConfigError, match="Not a boolean value"):
        config.parse_bool("perhaps")


def test_validate_endpoint_adds_trailing_slash():
    assert config.validate_endpoint("http://localhost:8080/fishnet") == "http://localhost:8080/fishnet/"


def test_validate_endpoint_rejects_non_http_scheme():
    with pytest.raises(ConfigError, match="http:// or https://"):
        config.validate_endpoint("ftp://example.org/fishnet")


def test_validate_key_allows_empty_key_for_nonproduction():
    conf = make_conf(Endpoint="http://localhost:8080/fishnet/")
    assert config.validate_key("", conf) == ""


def test_validate_key_requires_key_for_production():
    conf = make_conf(Endpoint="https://www.pychess.org/fishnet/")
    with pytest.raises(ConfigError, match="Fishnet key required"):
        config.validate_key("", conf)


def test_validate_key_strips_network_opt_out_suffix():
    conf = make_conf(Endpoint="https://www.pychess.org/fishnet/")
    assert config.validate_key("abc123!", conf, network=True) == "abc123"


def test_validate_key_rejects_non_alphanumeric():
    conf = make_conf(Endpoint="http://localhost/")
    with pytest.raises(ConfigError, match="alphanumeric"):
        config.validate_key("bad-key", conf)


def test_conf_get_supports_defaults_and_sections():
    conf = configparser.ConfigParser()
    assert config.conf_get(conf, "Missing", "fallback") == "fallback"
    conf.add_section("Other")
    conf.set("Other", "Value", "42")
    assert config.conf_get(conf, "Value", section="Other") == "42"


def test_validate_cores_auto_and_all(monkeypatch):
    monkeypatch.setattr(config.multiprocessing, "cpu_count", lambda: 8)
    assert config.validate_cores("auto") == 7
    assert config.validate_cores("all") == 8


def test_validate_cores_bounds(monkeypatch):
    monkeypatch.setattr(config.multiprocessing, "cpu_count", lambda: 4)
    with pytest.raises(ConfigError, match="at least one"):
        config.validate_cores("0")
    with pytest.raises(ConfigError, match="At most 4"):
        config.validate_cores("5")


def test_validate_threads_defaults_to_available_cores(monkeypatch):
    monkeypatch.setattr(config.multiprocessing, "cpu_count", lambda: 2)
    conf = make_conf(Cores="all")
    assert config.validate_threads("auto", conf) == 2


def test_validate_threads_rejects_more_than_cores(monkeypatch):
    monkeypatch.setattr(config.multiprocessing, "cpu_count", lambda: 4)
    conf = make_conf(Cores="2")
    with pytest.raises(ConfigError, match="not enough"):
        config.validate_threads("3", conf)


def test_validate_memory_auto_uses_process_count(monkeypatch):
    monkeypatch.setattr(config.multiprocessing, "cpu_count", lambda: 8)
    conf = make_conf(Cores="8", Threads="2")
    assert config.validate_memory("auto", conf) == 4 * config.HASH_DEFAULT


def test_validate_memory_enforces_minimum_and_maximum(monkeypatch):
    monkeypatch.setattr(config.multiprocessing, "cpu_count", lambda: 4)
    conf = make_conf(Cores="4", Threads="2")
    with pytest.raises(ConfigError, match="Not enough memory"):
        config.validate_memory("31", conf)
    with pytest.raises(ConfigError, match="Cannot reasonably use"):
        config.validate_memory(str(2 * config.HASH_MAX + 1), conf)


def test_start_backoff_fixed_stays_bounded(monkeypatch):
    monkeypatch.setattr(config.random, "random", lambda: 0.5)
    values = config.start_backoff(make_conf(FixedBackoff="true"))
    assert [next(values), next(values)] == [config.MAX_FIXED_BACKOFF / 2] * 2


def test_start_backoff_incremental_caps(monkeypatch):
    monkeypatch.setattr(config.random, "random", lambda: 0.0)
    values = config.start_backoff(make_conf(FixedBackoff="false"))
    observed = [next(values) for _ in range(int(config.MAX_BACKOFF) + 3)]
    assert observed[:3] == [0.5, 1.0, 1.5]
    assert observed[-1] == config.MAX_BACKOFF / 2


def test_load_conf_ignores_legacy_variant_path(tmp_path):
    config_file = tmp_path / "fishnet.ini"
    config_file.write_text(
        "[Fishnet]\nEngineDir = %s\n\n[Stockfish]\nVariantPath = variants.ini\nHash = 64\n" % tmp_path
    )

    class Args:
        no_conf = False
        conf = str(config_file)
        engine_dir = None
        stockfish_command = None
        key = None
        cores = None
        memory = None
        threads = None
        endpoint = None
        fixed_backoff = None
        setoption = []

    conf = config.load_conf(Args())
    assert not conf.has_option("Stockfish", "VariantPath")
    assert conf.get("Stockfish", "Hash") == "64"


def test_validate_stockfish_command_checks_builtin_and_custom_variant_support(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    process = object()
    supported = set(config.required_engine_variants)
    calls = []
    responses = iter((({}, supported), ({}, supported | {"fishnet-smoke"})))
    monkeypatch.setattr(config, "open_process", lambda command, engine_dir: process)
    monkeypatch.setattr(config, "uci", lambda current: next(responses))
    monkeypatch.setattr(config, "setoption", lambda current, name, value: calls.append((name, value)))
    monkeypatch.setattr(config, "kill_process", lambda current: calls.append(("kill", current)))

    assert config.validate_stockfish_command("./stockfish", conf) == "./stockfish"
    assert calls[0][0] == "VariantPath"
    assert calls[0][1].startswith(".fairyfishnet-variant-smoke-")
    assert calls[-1] == ("kill", process)
    assert not list(tmp_path.glob(".fairyfishnet-variant-smoke-*.ini"))


def test_validate_stockfish_command_rejects_engine_without_custom_ini_support(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    supported = set(config.required_engine_variants)
    responses = iter((({}, supported), ({}, supported)))
    monkeypatch.setattr(config, "open_process", lambda command, engine_dir: object())
    monkeypatch.setattr(config, "uci", lambda current: next(responses))
    monkeypatch.setattr(config, "setoption", lambda *args: None)
    monkeypatch.setattr(config, "kill_process", lambda current: None)

    with pytest.raises(ConfigError, match="does not support loading"):
        config.validate_stockfish_command("./stockfish", conf)
