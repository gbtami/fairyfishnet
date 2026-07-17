# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Content-addressed server variants.ini cache management."""

import collections
import contextlib
import errno
import hashlib
import logging
import os
import random
import re
import threading
import time

from .config import get_endpoint, get_engine_dir, get_key
from .constants import HTTP_TIMEOUT
from .dependencies import requests, sf
from .engine import isready, setoption
from .errors import ConfigError, JsonResponseError, VariantsIniError
from .http_utils import response_json

VARIANTS_CACHE_MAX_FILES = 512
VARIANTS_CACHE_MIN_AGE = 7 * 24 * 60 * 60
VARIANTS_CACHE_CLEANUP_INTERVAL = 24 * 60 * 60
VARIANTS_CACHE_LEASE_TTL = 15 * 60
VARIANTS_CACHE_LOCK_STALE = 10
VARIANTS_CACHE_FILE_RE = re.compile(r"^variants-([0-9a-f]{64})\.ini$")
VARIANTS_CACHE_LEASE_RE = re.compile(r"^\.fairyfishnet-variants-[0-9]+-[0-9a-f]+\.lease$")
VARIANTS_CACHE_THREAD_LOCK = threading.RLock()
VARIANTS_LEASE_LOCK = threading.RLock()
PYFFISH_VARIANT_LOCK = threading.RLock()
PYFFISH_LOADED_VARIANTS_SHA256 = set()
BUILTIN_VARIANTS = frozenset(str(variant).lower() for variant in sf.variants())
ENGINE_LOADED_VARIANTS_SHA256_ATTRIBUTE = "_fairyfishnet_loaded_variants_sha256"
ACTIVE_VARIANTS = collections.Counter()
VARIANTS_LEASE_TOKEN = "%d-%x" % (os.getpid(), int(time.time() * 1000000))
VariantsIni = collections.namedtuple("VariantsIni", "sha256 filename path")


def _valid_variants_sha256(value):
    return isinstance(value, str) and re.match(r"^[0-9a-f]{64}$", value) is not None


def _engine_variant_name(variant):
    name = str(variant or "standard").lower()
    if name in ("standard", "fromposition", "chess960"):
        return "chess"
    return name


def is_builtin_variant(variant):
    """Return whether a work unit can run without a server variants.ini."""

    return _engine_variant_name(variant) in BUILTIN_VARIANTS


def variants_ini_filename(sha256):
    if not _valid_variants_sha256(sha256):
        raise VariantsIniError("Invalid or missing variantsSha256 in fishnet job")
    return "variants-%s.ini" % sha256


def _variants_ini_entry(conf, sha256):
    filename = variants_ini_filename(sha256)
    return VariantsIni(sha256, filename, os.path.join(get_engine_dir(conf), filename))


def _atomic_write_text(path, text):
    temporary = "%s.tmp-%d-%d-%x" % (
        path,
        os.getpid(),
        id(threading.current_thread()),
        random.getrandbits(32),
    )
    try:
        with open(temporary, "w") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise


def _touch(path):
    try:
        os.utime(path, None)
    except OSError as err:
        if err.errno != errno.ENOENT:
            raise


@contextlib.contextmanager
def _variants_cache_lock(conf, timeout=VARIANTS_CACHE_LOCK_STALE + 1.0):
    lock_path = os.path.join(get_engine_dir(conf), ".fairyfishnet-variants-cache.lock")
    lock_token = "%s-%x" % (VARIANTS_LEASE_TOKEN, random.getrandbits(64))
    deadline = time.time() + timeout
    acquired = False

    with VARIANTS_CACHE_THREAD_LOCK:
        while not acquired:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError as err:
                if err.errno != errno.EEXIST:
                    raise

                try:
                    stale = time.time() - os.path.getmtime(lock_path) > VARIANTS_CACHE_LOCK_STALE
                except OSError:
                    stale = False

                if stale:
                    try:
                        os.remove(lock_path)
                    except OSError:
                        pass
                    continue

                if time.time() >= deadline:
                    raise IOError("Timed out waiting for variants.ini cache lock")
                time.sleep(0.05)
            else:
                with os.fdopen(descriptor, "w") as lock_file:
                    lock_file.write(lock_token)
                acquired = True

        try:
            yield
        finally:
            try:
                with open(lock_path) as lock_file:
                    owner = lock_file.read()
            except OSError:
                owner = None
            if owner == lock_token:
                try:
                    os.remove(lock_path)
                except OSError as err:
                    if err.errno != errno.ENOENT:
                        logging.warning("Could not remove variants.ini cache lock: %s", err)


def write_variants_ini(conf, ini_text, sha256):
    if not isinstance(ini_text, str):
        raise VariantsIniError("Fishnet variants endpoint returned a non-text variantsIni payload")
    actual_sha256 = hashlib.sha256(ini_text.encode("utf-8")).hexdigest()
    if actual_sha256 != sha256:
        raise VariantsIniError(
            "Fishnet variants.ini content hash %s does not match expected %s" % (actual_sha256, sha256)
        )

    entry = _variants_ini_entry(conf, sha256)
    with _variants_cache_lock(conf):
        if not os.path.exists(entry.path):
            _atomic_write_text(entry.path, ini_text)
        else:
            _touch(entry.path)
    return entry


def _load_cached_variants_ini(conf, expected_sha256):
    entry = _variants_ini_entry(conf, expected_sha256)
    with _variants_cache_lock(conf):
        if not os.path.exists(entry.path):
            return None
        _touch(entry.path)
    return entry


def sync_variants_ini(conf, expected_sha256, variant=None):
    """Return the exact immutable variants.ini entry required by a work unit."""

    if not _valid_variants_sha256(expected_sha256):
        raise VariantsIniError("Fishnet job did not provide a valid variantsSha256")

    try:
        cached = _load_cached_variants_ini(conf, expected_sha256)
    except IOError as err:
        raise VariantsIniError("Could not read variants.ini cache: %s" % err) from err
    if cached:
        return cached

    key = get_key(conf)
    if not key:
        raise VariantsIniError("Cannot download variants.ini without a fishnet key")

    params = {"sha256": expected_sha256}
    if variant:
        params["variant"] = variant

    try:
        response = requests.get(get_endpoint(conf, "variants/%s" % key), params=params, timeout=HTTP_TIMEOUT)
        if response.status_code == 404:
            raise ConfigError("Invalid or inactive fishnet key")
        if response.status_code == 409:
            raise VariantsIniError("Server no longer has the exact variants.ini required by this job")
        response.raise_for_status()
        payload = response_json(response, "fishnet variants.ini")
        ini_text = payload["variantsIni"]
        response_sha256 = payload.get("variantsSha256")
        if response_sha256 != expected_sha256:
            raise VariantsIniError(
                "Server returned variants.ini hash %s, but job requires %s"
                % (response_sha256 or "<missing>", expected_sha256)
            )
        entry = write_variants_ini(conf, ini_text, expected_sha256)
        logging.info("Downloaded variants.ini from server (%s)", expected_sha256[:12])
        return entry
    except VariantsIniError:
        raise
    except JsonResponseError as err:
        raise VariantsIniError(str(err)) from err
    except ConfigError:
        raise
    except Exception as err:
        raise VariantsIniError("Could not fetch required variants.ini from server: %s" % err) from err


def _variants_lease_path(conf):
    return os.path.join(get_engine_dir(conf), ".fairyfishnet-variants-%s.lease" % VARIANTS_LEASE_TOKEN)


def _active_variants_for_engine_dir(engine_dir):
    return sorted(
        sha256
        for (active_engine_dir, sha256), count in ACTIVE_VARIANTS.items()
        if active_engine_dir == engine_dir and count > 0
    )


def _write_variants_lease(conf):
    engine_dir = get_engine_dir(conf)
    path = _variants_lease_path(conf)
    active = _active_variants_for_engine_dir(engine_dir)
    if active:
        _atomic_write_text(path, "".join("%s\n" % sha256 for sha256 in active))
    else:
        try:
            os.remove(path)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise


def _try_write_variants_lease(conf):
    try:
        _write_variants_lease(conf)
    except OSError as err:
        logging.warning("Could not update variants.ini cache lease: %s", err)


def refresh_variants_ini_lease(conf):
    with VARIANTS_LEASE_LOCK:
        _try_write_variants_lease(conf)


def clear_variants_ini_lease(conf):
    engine_dir = get_engine_dir(conf)
    with VARIANTS_LEASE_LOCK:
        for key in list(ACTIVE_VARIANTS):
            if key[0] == engine_dir:
                del ACTIVE_VARIANTS[key]
        _try_write_variants_lease(conf)


@contextlib.contextmanager
def active_variants_ini(conf, entry):
    key = (get_engine_dir(conf), entry.sha256)
    with VARIANTS_LEASE_LOCK:
        ACTIVE_VARIANTS[key] += 1
        _try_write_variants_lease(conf)
    try:
        yield
    finally:
        with VARIANTS_LEASE_LOCK:
            ACTIVE_VARIANTS[key] -= 1
            if ACTIVE_VARIANTS[key] <= 0:
                del ACTIVE_VARIANTS[key]
            _try_write_variants_lease(conf)


def _leased_variants_ini_hashes(engine_dir, now):
    protected = set()
    try:
        names = os.listdir(engine_dir)
    except OSError:
        return protected

    for name in names:
        if VARIANTS_CACHE_LEASE_RE.match(name) is None:
            continue
        path = os.path.join(engine_dir, name)
        try:
            modified = os.path.getmtime(path)
        except OSError:
            continue
        if now - modified > VARIANTS_CACHE_LEASE_TTL:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            with open(path) as lease_file:
                for line in lease_file:
                    sha256 = line.strip()
                    if _valid_variants_sha256(sha256):
                        protected.add(sha256)
        except OSError:
            continue
    return protected


def cleanup_variants_ini_cache(conf, now=None, max_files=VARIANTS_CACHE_MAX_FILES, min_age=VARIANTS_CACHE_MIN_AGE):
    now = time.time() if now is None else now
    engine_dir = get_engine_dir(conf)
    deleted = 0

    try:
        with _variants_cache_lock(conf):
            protected = _leased_variants_ini_hashes(engine_dir, now)
            with VARIANTS_LEASE_LOCK:
                protected.update(_active_variants_for_engine_dir(engine_dir))

            entries = []
            for name in os.listdir(engine_dir):
                match = VARIANTS_CACHE_FILE_RE.match(name)
                if match is None:
                    continue
                path = os.path.join(engine_dir, name)
                try:
                    modified = os.path.getmtime(path)
                except OSError:
                    continue
                entries.append((modified, match.group(1), path))

            entries.sort(reverse=True)
            retained = {sha256 for _, sha256, _ in entries[:max_files]}
            for modified, sha256, path in entries:
                if sha256 in protected or sha256 in retained or now - modified < min_age:
                    continue
                try:
                    os.remove(path)
                    deleted += 1
                except OSError as err:
                    if err.errno != errno.ENOENT:
                        logging.warning("Could not remove cached variants.ini %s: %s", path, err)
    except IOError as err:
        logging.warning("Skipping variants.ini cache cleanup: %s", err)
        return 0

    if deleted:
        logging.info("Removed %d old cached variants.ini file(s)", deleted)
    return deleted


def _apply_engine_variants(p, entry):
    """Load an exact variants file into one engine process at most once per hash."""

    if p is None:
        return
    loaded_sha256 = getattr(p, ENGINE_LOADED_VARIANTS_SHA256_ATTRIBUTE, set())
    if entry.sha256 in loaded_sha256:
        return

    setoption(p, "VariantPath", entry.filename)
    isready(p)
    try:
        setattr(p, ENGINE_LOADED_VARIANTS_SHA256_ATTRIBUTE, loaded_sha256 | {entry.sha256})
    except (AttributeError, TypeError):
        # Tests and third-party process wrappers may not allow custom attributes.
        # A real subprocess.Popen instance does, so production workers still avoid
        # redundant VariantPath reloads.
        pass


def reload_engine_variants(p, conf, expected_sha256, variant=None):
    entry = sync_variants_ini(conf, expected_sha256, variant)
    _apply_engine_variants(p, entry)
    return entry


@contextlib.contextmanager
def use_engine_variants(p, conf, expected_sha256, variant=None):
    if is_builtin_variant(variant):
        yield None
        return

    if not expected_sha256:
        raise VariantsIniError(
            "Fishnet job for non-built-in variant %s did not provide variantsSha256"
            % (variant or "<missing>")
        )

    entry = sync_variants_ini(conf, expected_sha256, variant)
    with active_variants_ini(conf, entry):
        _apply_engine_variants(p, entry)
        yield entry


def pyffish_get_fen(entry, variant, fen, moves, chess960, sfen, show_promoted):
    with PYFFISH_VARIANT_LOCK:
        if entry is not None and entry.sha256 not in PYFFISH_LOADED_VARIANTS_SHA256:
            sf.set_option("VariantPath", entry.path)
            PYFFISH_LOADED_VARIANTS_SHA256.add(entry.sha256)
        return sf.get_fen(variant, fen, moves, chess960, sfen, show_promoted)
