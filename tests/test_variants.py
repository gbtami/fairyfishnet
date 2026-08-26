import hashlib
import os
import time
from types import SimpleNamespace

import pytest

import fairyfishnet.variants as variants
from fairyfishnet.errors import ConfigError, EngineVariantConflict, VariantsIniError
from tests.helpers import make_conf


def test_variants_ini_filename_requires_lowercase_sha256():
    digest = "a" * 64
    assert variants.variants_ini_filename(digest) == "variants-%s.ini" % digest
    with pytest.raises(VariantsIniError, match="Invalid or missing"):
        variants.variants_ini_filename("A" * 64)
    with pytest.raises(VariantsIniError, match="Invalid or missing"):
        variants.variants_ini_filename("short")


def test_scoped_write_verifies_content_hash(tmp_path):
    conf = make_conf(tmp_path)
    payload = "[custom]\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    entry = variants.write_variants_ini(conf, payload, digest)
    assert entry.sha256 == digest
    assert entry.filename == "variants-%s.ini" % digest
    assert open(entry.path).read() == payload


def test_scoped_write_rejects_hash_mismatch(tmp_path):
    conf = make_conf(tmp_path)
    with pytest.raises(VariantsIniError, match="does not match expected"):
        variants.write_variants_ini(conf, "[custom]\n", "a" * 64)


def test_scoped_write_is_content_addressed_and_reused(tmp_path):
    conf = make_conf(tmp_path)
    first = "[first]\n"
    digest = hashlib.sha256(first.encode("utf-8")).hexdigest()
    entry = variants.write_variants_ini(conf, first, digest)
    variants.write_variants_ini(conf, first, digest)
    assert open(entry.path).read() == first


def test_sync_requires_job_hash(tmp_path):
    conf = make_conf(tmp_path)
    with pytest.raises(VariantsIniError, match="did not provide"):
        variants.sync_variants_ini(conf, None)


def test_builtin_variant_names_use_initial_pyffish_catalog(monkeypatch):
    monkeypatch.setattr(variants, "BUILTIN_VARIANTS", frozenset(("chess", "crazyhouse")))
    assert variants.is_builtin_variant("chess")
    assert variants.is_builtin_variant("standard")
    assert variants.is_builtin_variant("chess960")
    assert variants.is_builtin_variant("CRAZYHOUSE")
    assert not variants.is_builtin_variant("grandhouse")


def test_use_engine_variants_skips_ini_for_builtin_variant(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    monkeypatch.setattr(variants, "BUILTIN_VARIANTS", frozenset(("chess",)))
    monkeypatch.setattr(
        variants,
        "sync_variants_ini",
        lambda *args, **kwargs: pytest.fail("built-in variants must not fetch an INI"),
    )

    with variants.use_engine_variants(SimpleNamespace(), conf, None, "standard") as entry:
        assert entry is None


def test_use_engine_variants_requires_hash_for_custom_variant(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    monkeypatch.setattr(variants, "BUILTIN_VARIANTS", frozenset(("chess",)))

    with pytest.raises(VariantsIniError, match="non-built-in variant grandhouse"):
        with variants.use_engine_variants(SimpleNamespace(), conf, None, "grandhouse"):
            pass


def test_use_engine_variants_ignores_server_hash_for_builtin_variant(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    payload = "[server-rules]\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    monkeypatch.setattr(variants, "BUILTIN_VARIANTS", frozenset(("chess",)))
    monkeypatch.setattr(
        variants,
        "sync_variants_ini",
        lambda *args, **kwargs: pytest.fail("built-in variants must ignore custom INI metadata"),
    )

    with variants.use_engine_variants(SimpleNamespace(), conf, digest, "chess") as entry:
        assert entry is None


def test_sync_uses_cached_entry_without_network(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")
    payload = "[cached]\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    expected = variants.write_variants_ini(conf, payload, digest)
    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: pytest.fail("network should not be used"))
    assert variants.sync_variants_ini(conf, digest) == expected


def test_sync_downloads_and_caches_exact_payload(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")
    payload = "[downloaded]\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    class Response:
        status_code = 200
        reason = "OK"
        headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            return None

        def json(self):
            return {"variantsIni": payload, "variantsSha256": digest}

    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: Response())
    entry = variants.sync_variants_ini(conf, digest, variant="custom")
    assert entry.sha256 == digest
    assert open(entry.path).read() == payload


def test_sync_rejects_server_hash_mismatch(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")
    expected = "a" * 64

    class Response:
        status_code = 200
        reason = "OK"
        headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            return None

        def json(self):
            return {"variantsIni": "[new]\n", "variantsSha256": "b" * 64}

    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(VariantsIniError, match="job requires"):
        variants.sync_variants_ini(conf, expected)


def test_sync_rejects_unavailable_exact_payload(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")

    class Response:
        status_code = 409
        reason = "Conflict"
        headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(VariantsIniError, match="no longer has"):
        variants.sync_variants_ini(conf, "c" * 64)


def test_sync_rejects_inactive_key(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")

    class Response:
        status_code = 404
        reason = "Not Found"
        headers = {}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(ConfigError, match="Invalid or inactive"):
        variants.sync_variants_ini(conf, "c" * 64)


def test_reload_engine_variants_uses_exact_cache_entry(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    payload = "[custom]\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    entry = variants.write_variants_ini(conf, payload, digest)
    process = SimpleNamespace()
    calls = []
    monkeypatch.setattr(variants, "setoption", lambda process, name, value: calls.append((name, value)))
    monkeypatch.setattr(variants, "isready", lambda process: None)
    selected = variants.reload_engine_variants(process, conf, expected_sha256=digest)
    assert selected == entry
    assert calls == [("VariantPath", entry.filename)]
    assert getattr(process, variants.ENGINE_LOADED_VARIANTS_SHA256_ATTRIBUTE) == {digest}


def test_reload_engine_variants_skips_hash_already_loaded_by_process(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    payload = "[custom]\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    variants.write_variants_ini(conf, payload, digest)
    process = SimpleNamespace()
    calls = []
    monkeypatch.setattr(variants, "setoption", lambda process, name, value: calls.append((name, value)))
    monkeypatch.setattr(variants, "isready", lambda process: calls.append(("isready", None)))

    variants.reload_engine_variants(process, conf, expected_sha256=digest)
    variants.reload_engine_variants(process, conf, expected_sha256=digest)

    assert calls == [("VariantPath", variants.variants_ini_filename(digest)), ("isready", None)]


def test_reload_engine_variants_loads_a_new_hash(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    payloads = ["[first]\n", "[second]\n"]
    digests = [hashlib.sha256(payload.encode("utf-8")).hexdigest() for payload in payloads]
    for payload, digest in zip(payloads, digests):
        variants.write_variants_ini(conf, payload, digest)
    process = SimpleNamespace()
    calls = []
    monkeypatch.setattr(variants, "setoption", lambda process, name, value: calls.append((name, value)))
    monkeypatch.setattr(variants, "isready", lambda process: None)

    for digest in [digests[0], digests[1], digests[0]]:
        variants.reload_engine_variants(process, conf, expected_sha256=digest)

    assert calls == [
        ("VariantPath", variants.variants_ini_filename(digests[0])),
        ("VariantPath", variants.variants_ini_filename(digests[1])),
    ]


def test_reload_engine_variants_rejects_changed_rules_under_loaded_name(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    payloads = ["[custom:chess]\ncustomPiece1 = a:N\n", "[custom:chess]\ncustomPiece1 = a:B\n"]
    digests = [hashlib.sha256(payload.encode("utf-8")).hexdigest() for payload in payloads]
    for payload, digest in zip(payloads, digests):
        variants.write_variants_ini(conf, payload, digest)
    process = SimpleNamespace()
    calls = []
    monkeypatch.setattr(variants, "setoption", lambda process, name, value: calls.append((name, value)))
    monkeypatch.setattr(variants, "isready", lambda process: None)

    variants.reload_engine_variants(process, conf, expected_sha256=digests[0])
    with pytest.raises(EngineVariantConflict, match="custom"):
        variants.reload_engine_variants(process, conf, expected_sha256=digests[1])

    assert calls == [("VariantPath", variants.variants_ini_filename(digests[0]))]


def test_reload_engine_variants_allows_identical_shared_section(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    payloads = [
        "[base:chess]\ncustomPiece1 = a:N\n\n[first:base]\n",
        "[base:chess]\ncustomPiece1 = a:N\n\n[second:base]\n",
    ]
    digests = [hashlib.sha256(payload.encode("utf-8")).hexdigest() for payload in payloads]
    for payload, digest in zip(payloads, digests):
        variants.write_variants_ini(conf, payload, digest)
    process = SimpleNamespace()
    calls = []
    monkeypatch.setattr(variants, "setoption", lambda process, name, value: calls.append((name, value)))
    monkeypatch.setattr(variants, "isready", lambda process: None)

    for digest in digests:
        variants.reload_engine_variants(process, conf, expected_sha256=digest)

    assert calls == [("VariantPath", variants.variants_ini_filename(digest)) for digest in digests]


def test_active_entry_is_protected_from_cleanup(tmp_path):
    conf = make_conf(tmp_path)
    now = time.time()
    active_payload = "[active]\n"
    inactive_payload = "[inactive]\n"
    active = variants.write_variants_ini(
        conf, active_payload, hashlib.sha256(active_payload.encode("utf-8")).hexdigest()
    )
    inactive = variants.write_variants_ini(
        conf, inactive_payload, hashlib.sha256(inactive_payload.encode("utf-8")).hexdigest()
    )
    os.utime(active.path, (now - 100, now - 100))
    os.utime(inactive.path, (now - 100, now - 100))

    with variants.active_variants_ini(conf, active):
        assert variants.cleanup_variants_ini_cache(conf, now=now, max_files=0, min_age=0) == 1
        assert os.path.exists(active.path)
        assert not os.path.exists(inactive.path)


def test_cleanup_keeps_newest_entries(tmp_path):
    conf = make_conf(tmp_path)
    now = time.time()
    older_payload = "[older]\n"
    newer_payload = "[newer]\n"
    older = variants.write_variants_ini(conf, older_payload, hashlib.sha256(older_payload.encode("utf-8")).hexdigest())
    newer = variants.write_variants_ini(conf, newer_payload, hashlib.sha256(newer_payload.encode("utf-8")).hexdigest())
    os.utime(older.path, (now - 200, now - 200))
    os.utime(newer.path, (now - 100, now - 100))
    assert variants.cleanup_variants_ini_cache(conf, now=now, max_files=1, min_age=0) == 1
    assert not os.path.exists(older.path)
    assert os.path.exists(newer.path)


def test_cleanup_keeps_recent_and_unrelated_files(tmp_path):
    conf = make_conf(tmp_path)
    payload = "[recent]\n"
    variants.write_variants_ini(conf, payload, hashlib.sha256(payload.encode("utf-8")).hexdigest())
    unrelated = tmp_path / "variants.ini"
    unrelated.write_text("operator-owned\n")
    assert variants.cleanup_variants_ini_cache(conf, now=time.time(), max_files=0, min_age=60) == 0
    assert unrelated.read_text() == "operator-owned\n"
