import hashlib
import os
import time

import pytest

import fairyfishnet.variants as variants
from fairyfishnet.errors import ConfigError
from tests.helpers import make_conf


def test_variants_ini_filename_accepts_only_lowercase_sha256():
    digest = "a" * 64
    assert variants.variants_ini_filename(digest) == "variants-%s.ini" % digest
    assert variants.variants_ini_filename("A" * 64) == "variants.ini"
    assert variants.variants_ini_filename("short") == "variants.ini"


def test_write_default_variants_ini_records_payload_hash(tmp_path):
    conf = make_conf(tmp_path)
    payload = "[custom]\n"
    entry = variants.write_variants_ini(conf, payload)
    assert entry.sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert entry.filename == "variants.ini"
    assert (tmp_path / "variants.ini").read_text() == payload


def test_scoped_write_is_content_addressed_and_reused(tmp_path):
    conf = make_conf(tmp_path)
    digest = "a" * 64
    entry = variants.write_variants_ini(conf, "[first]\n", digest, scoped=True)
    variants.write_variants_ini(conf, "[second]\n", digest, scoped=True)
    assert open(entry.path).read() == "[first]\n"


def test_sync_without_hash_uses_default_entry(tmp_path):
    conf = make_conf(tmp_path)
    assert variants.sync_variants_ini(conf) == variants.default_variants_ini(conf)


def test_sync_uses_cached_scoped_entry_without_network(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")
    digest = "b" * 64
    expected = variants.write_variants_ini(conf, "[cached]\n", digest, scoped=True)
    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: pytest.fail("network should not be used"))
    assert variants.sync_variants_ini(conf, digest) == expected


def test_sync_downloads_and_caches_payload(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")
    payload = "[downloaded]\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"variantsIni": payload, "variantsSha256": digest}

    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: Response())
    entry = variants.sync_variants_ini(conf, digest, required=True, variant="custom")
    assert entry.sha256 == digest
    assert open(entry.path).read() == payload


def test_sync_rejects_inactive_key_when_required(tmp_path, monkeypatch):
    conf = make_conf(tmp_path, Key="key", Endpoint="https://example.org/fishnet/")

    class Response:
        status_code = 404

        def raise_for_status(self):
            return None

    monkeypatch.setattr(variants.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(ConfigError, match="Invalid or inactive"):
        variants.sync_variants_ini(conf, "c" * 64, required=True)


def test_reload_engine_variants_uses_selected_cache_entry(tmp_path, monkeypatch):
    conf = make_conf(tmp_path)
    digest = "d" * 64
    entry = variants.write_variants_ini(conf, "[custom]\n", digest, scoped=True)
    calls = []
    monkeypatch.setattr(variants, "setoption", lambda process, name, value: calls.append((name, value)))
    monkeypatch.setattr(variants, "isready", lambda process: None)
    selected = variants.reload_engine_variants(object(), conf, expected_sha256=digest)
    assert selected == entry
    assert calls == [("VariantPath", entry.filename)]


def test_active_entry_is_protected_from_cleanup(tmp_path):
    conf = make_conf(tmp_path)
    now = time.time()
    active = variants.write_variants_ini(conf, "[active]\n", "e" * 64, scoped=True)
    inactive = variants.write_variants_ini(conf, "[inactive]\n", "f" * 64, scoped=True)
    os.utime(active.path, (now - 100, now - 100))
    os.utime(inactive.path, (now - 100, now - 100))

    with variants.active_variants_ini(conf, active):
        assert variants.cleanup_variants_ini_cache(conf, now=now, max_files=0, min_age=0) == 1
        assert os.path.exists(active.path)
        assert not os.path.exists(inactive.path)


def test_cleanup_keeps_newest_entries(tmp_path):
    conf = make_conf(tmp_path)
    now = time.time()
    older = variants.write_variants_ini(conf, "[older]\n", "1" * 64, scoped=True)
    newer = variants.write_variants_ini(conf, "[newer]\n", "2" * 64, scoped=True)
    os.utime(older.path, (now - 200, now - 200))
    os.utime(newer.path, (now - 100, now - 100))
    assert variants.cleanup_variants_ini_cache(conf, now=now, max_files=1, min_age=0) == 1
    assert not os.path.exists(older.path)
    assert os.path.exists(newer.path)


def test_cleanup_keeps_recent_and_default_files(tmp_path):
    conf = make_conf(tmp_path)
    variants.write_variants_ini(conf, "[recent]\n", "3" * 64, scoped=True)
    default = variants.write_variants_ini(conf, "[base]\n")
    assert variants.cleanup_variants_ini_cache(conf, now=time.time(), max_files=0, min_age=60) == 0
    assert os.path.exists(default.path)


def test_create_variants_ini_writes_embedded_site_variants(tmp_path):
    class Args:
        no_conf = True
        conf = None
        engine_dir = str(tmp_path)
        setoption = []

    variants.create_variants_ini(Args())
    content = (tmp_path / "variants.ini").read_text()
    assert "[grandhouse:grand]" in content
    assert "[cannonshogi:shogi]" in content
