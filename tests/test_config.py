import configparser
import hashlib

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


def test_update_nnue_downloads_verified_catalogue_assets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "NNUE_NET", {})

    payloads = {
        "3check": b"variant-network",
        "nn": b"standard-chess-network",
    }
    entries = []
    for network_id, payload in payloads.items():
        digest = hashlib.sha256(payload).hexdigest()
        filename = "%s-%s.nnue" % (network_id, digest[:12])
        entries.append(
            {
                "id": network_id,
                "file": filename,
                "bytes": len(payload),
                "sha256": digest,
                "url": config.NNUE_CATALOGUE_RELEASE_PREFIX + filename,
            }
        )
    manifest = {"schema": 1, "networks": entries}
    calls = []
    progress_bars = []

    class Progress:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.updates = []
            progress_bars.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def update(self, amount):
            self.updates.append(amount)

    class Response:
        def __init__(self, json_data=None, content=None):
            self.json_data = json_data
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return self.json_data

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield self.content

        def close(self):
            pass

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if url == config.NNUE_CATALOGUE_MANIFEST:
            return Response(json_data=manifest)
        network_id = "3check" if "3check-" in url else "nn"
        return Response(content=payloads[network_id])

    monkeypatch.setattr(config.requests, "get", get)
    monkeypatch.setattr(config, "tqdm", Progress)

    config.update_nnue()

    assert config.NNUE_NET == {
        network_id: hashlib.sha256(payload).hexdigest()[:12] for network_id, payload in payloads.items()
    }
    assert {path.name for path in tmp_path.glob("*.nnue")} == {entry["file"] for entry in entries}
    assert [path.read_bytes() for path in sorted(tmp_path.glob("*.nnue"))] == list(payloads.values())
    assert calls[0] == (config.NNUE_CATALOGUE_MANIFEST, {"timeout": config.HTTP_TIMEOUT})
    assert all(call[1] == {"timeout": config.HTTP_TIMEOUT, "stream": True} for call in calls[1:])
    assert [progress.kwargs["total"] for progress in progress_bars] == [len(payload) for payload in payloads.values()]
    assert [progress.updates for progress in progress_bars] == [[len(payload)] for payload in payloads.values()]
    assert not list(tmp_path.glob(".*.nnue.*"))


def test_update_nnue_preserves_existing_network_when_hash_verification_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "NNUE_NET", {})
    expected = b"good"
    digest = hashlib.sha256(expected).hexdigest()
    filename = "3check-%s.nnue" % digest[:12]
    existing = tmp_path / filename
    existing.write_bytes(b"old")
    manifest = {
        "schema": 1,
        "networks": [
            {
                "id": "3check",
                "file": filename,
                "bytes": len(expected),
                "sha256": digest,
                "url": config.NNUE_CATALOGUE_RELEASE_PREFIX + filename,
            },
            {
                "id": "nn",
                "file": "nn-%s.nnue" % digest[:12],
                "bytes": len(expected),
                "sha256": digest,
                "url": config.NNUE_CATALOGUE_RELEASE_PREFIX + "nn-%s.nnue" % digest[:12],
            },
        ],
    }

    class Response:
        def __init__(self, json_data=None, content=None):
            self.json_data = json_data
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return self.json_data

        def iter_content(self, chunk_size):
            yield self.content

        def close(self):
            pass

    def get(url, **kwargs):
        if url == config.NNUE_CATALOGUE_MANIFEST:
            return Response(json_data=manifest)
        return Response(content=b"evil")

    monkeypatch.setattr(config.requests, "get", get)

    with pytest.raises(ConfigError, match="SHA-256 verification"):
        config.update_nnue()

    assert existing.read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.nnue.*"))


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


def test_validate_alice_stockfish_command_is_optional():
    assert config.validate_alice_stockfish_command("", make_conf()) is None


def test_validate_alice_stockfish_command_requires_alice_variant(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    process = object()
    killed = []
    monkeypatch.setattr(config, "open_process", lambda command, engine_dir: process)
    monkeypatch.setattr(config, "uci", lambda current: ({"name": "Stockfish"}, {"chess"}))
    monkeypatch.setattr(config, "kill_process", lambda current: killed.append(current))

    with pytest.raises(ConfigError, match="does not advertise the alice"):
        config.validate_alice_stockfish_command("./alice-stockfish", conf)

    assert killed == [process]


def test_validate_alice_stockfish_command_applies_dedicated_options(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    conf.add_section("AliceStockfish")
    conf.set("AliceStockfish", "Alice Evaluation", "Legacy")
    conf.set("AliceStockfish", "EvalFile", "Alice_v1.nnue")
    process = object()
    calls = []
    monkeypatch.setattr(config, "open_process", lambda command, engine_dir: process)
    monkeypatch.setattr(config, "uci", lambda current: ({"name": "Alice-Stockfish"}, {"alice"}))
    monkeypatch.setattr(config, "setoption", lambda current, name, value: calls.append((name, value)))
    monkeypatch.setattr(config, "isready", lambda current: calls.append(("isready", current)))
    monkeypatch.setattr(config, "current_fen", lambda current: "alice-start-fen")
    monkeypatch.setattr(
        config,
        "go",
        lambda current, position, moves, **limits: {"bestmove": "e2e4"},
    )
    monkeypatch.setattr(config, "kill_process", lambda current: calls.append(("kill", current)))

    assert config.validate_alice_stockfish_command("./alice-stockfish", conf) == "./alice-stockfish"
    assert ("alice evaluation", "Legacy") in calls
    assert ("evalfile", "Alice_v1.nnue") in calls
    assert ("UCI_Variant", "alice") not in calls
    assert calls[-1] == ("kill", process)


def test_validate_alice_stockfish_command_requires_working_search(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    process = object()
    killed = []
    monkeypatch.setattr(config, "open_process", lambda command, engine_dir: process)
    monkeypatch.setattr(config, "uci", lambda current: ({"name": "Alice-Stockfish"}, {"alice"}))
    monkeypatch.setattr(config, "setoption", lambda current, name, value: None)
    monkeypatch.setattr(config, "isready", lambda current: None)
    monkeypatch.setattr(config, "current_fen", lambda current: "alice-start-fen")
    monkeypatch.setattr(config, "go", lambda current, position, moves, **limits: {"bestmove": None})
    monkeypatch.setattr(config, "kill_process", lambda current: killed.append(current))

    with pytest.raises(ConfigError, match="search probe did not return a best move"):
        config.validate_alice_stockfish_command("./alice-stockfish", conf)

    assert killed == [process]
