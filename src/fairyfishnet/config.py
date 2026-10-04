# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Configuration loading, prompting, and validation."""

import configparser
import hashlib
import logging
import multiprocessing
import os
import random
import re
import sys
import tempfile
import urllib.parse as urlparse

from tqdm import tqdm

from .constants import (
    DEFAULT_CONFIG,
    DEFAULT_ENDPOINT,
    DEFAULT_THREADS,
    HASH_DEFAULT,
    HASH_MAX,
    HASH_MIN,
    HTTP_TIMEOUT,
    MAX_BACKOFF,
    MAX_FIXED_BACKOFF,
    NNUE_NET,
    nnue_variants,
    required_engine_variants,
)
from .dependencies import requests
from .engine import current_fen, go, isready, kill_process, open_process, setoption, uci
from .errors import ConfigError
from .http_utils import response_json
from .logging_utils import CensorLogFilter

NNUE_CATALOGUE_MANIFEST = "https://raw.githubusercontent.com/gbtami/Fairy-Stockfish-NNUE-Catalogue/main/manifest.json"
NNUE_CATALOGUE_RELEASE_PREFIX = "https://github.com/gbtami/Fairy-Stockfish-NNUE-Catalogue/releases/download/networks/"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NNUE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def load_conf(args):
    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.add_section("Stockfish")
    conf.add_section("AliceStockfish")

    if not args.no_conf:
        if not args.conf and not os.path.isfile(DEFAULT_CONFIG):
            return configure(args)

        config_file = args.conf or DEFAULT_CONFIG
        logging.debug("Using config file: %s", config_file)

        if not conf.read(config_file):
            raise ConfigError("Could not read config file: %s" % config_file)

    if hasattr(args, "engine_dir") and args.engine_dir is not None:
        conf.set("Fishnet", "EngineDir", args.engine_dir)
    if hasattr(args, "stockfish_command") and args.stockfish_command is not None:
        conf.set("Fishnet", "StockfishCommand", args.stockfish_command)
    if hasattr(args, "alice_stockfish_command") and args.alice_stockfish_command is not None:
        conf.set("Fishnet", "AliceStockfishCommand", args.alice_stockfish_command)
    if hasattr(args, "key") and args.key is not None:
        conf.set("Fishnet", "Key", args.key)
    if hasattr(args, "cores") and args.cores is not None:
        conf.set("Fishnet", "Cores", args.cores)
    if hasattr(args, "memory") and args.memory is not None:
        conf.set("Fishnet", "Memory", args.memory)
    if hasattr(args, "threads") and args.threads is not None:
        conf.set("Fishnet", "Threads", str(args.threads))
    if hasattr(args, "endpoint") and args.endpoint is not None:
        conf.set("Fishnet", "Endpoint", args.endpoint)
    if hasattr(args, "fixed_backoff") and args.fixed_backoff is not None:
        conf.set("Fishnet", "FixedBackoff", str(args.fixed_backoff))
    for option_name, option_value in args.setoption:
        conf.set("Stockfish", option_name.lower(), option_value)

    # VariantPath is selected per work unit from the server-provided content hash.
    # Ignore the legacy generated option when reading older configuration files.
    if conf.has_option("Stockfish", "VariantPath"):
        conf.remove_option("Stockfish", "VariantPath")

    logging.getLogger().addFilter(CensorLogFilter(conf_get(conf, "Key")))

    return conf


def config_input(prompt, validator, out):
    while True:
        if out == sys.stdout:
            inp = input(prompt)
        else:
            if prompt:
                out.write(prompt)
                out.flush()

            inp = input()

        try:
            return validator(inp)
        except ConfigError as error:
            print(error, file=out)


def configure(args):
    if sys.stdout.isatty():
        out = sys.stdout
        try:
            # Unix: Importing for its side effect
            import readline  # noqa: F401
        except ImportError:
            # Windows
            pass
    else:
        out = sys.stderr

    print(file=out)
    print("### Configuration", file=out)
    print(file=out)

    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.add_section("Stockfish")
    conf.add_section("AliceStockfish")

    # Ensure the config file is going to be writable
    config_file = os.path.abspath(args.conf or DEFAULT_CONFIG)
    if os.path.isfile(config_file):
        conf.read(config_file)
        with open(config_file, "r+"):
            pass
    else:
        with open(config_file, "w"):
            pass
        os.remove(config_file)

    if conf.has_option("Stockfish", "VariantPath"):
        conf.remove_option("Stockfish", "VariantPath")

    # Stockfish working directory
    engine_dir = config_input(
        "Engine working directory (default: %s): " % os.path.abspath("."), validate_engine_dir, out
    )
    conf.set("Fishnet", "EngineDir", engine_dir)

    # Stockfish command
    print(file=out)
    print("Fishnet uses a custom Fairy-Stockfish build with variant support.", file=out)
    print("Fairy-Stockfish is licensed under the GNU General Public License v3.", file=out)
    print("You can find the source at: https://github.com/ianfab/Fairy-Stockfish", file=out)
    print(file=out)
    print("You can build custom Fairy-Stockfish yourself and provide", file=out)
    print("the path or automatically download a precompiled binary.", file=out)
    print(file=out)
    stockfish_command = config_input(
        "Path or command (will download by default): ", lambda v: validate_stockfish_command(v, conf), out
    )
    if not stockfish_command:
        conf.remove_option("Fishnet", "StockfishCommand")
    else:
        conf.set("Fishnet", "StockfishCommand", stockfish_command)
    print(file=out)

    print("Alice Chess can optionally use a dedicated Alice-Stockfish engine.", file=out)
    print("Leave this blank to disable Alice support on this worker.", file=out)
    alice_stockfish_command = config_input(
        "Alice-Stockfish path or command (optional): ",
        lambda v: validate_alice_stockfish_command(v, conf),
        out,
    )
    if not alice_stockfish_command:
        conf.remove_option("Fishnet", "AliceStockfishCommand")
    else:
        conf.set("Fishnet", "AliceStockfishCommand", alice_stockfish_command)
    print(file=out)

    # Cores
    max_cores = multiprocessing.cpu_count()
    default_cores = max(1, max_cores - 1)
    cores = config_input(
        "Number of cores to use for engine threads (default %d, max %d): " % (default_cores, max_cores),
        validate_cores,
        out,
    )
    conf.set("Fishnet", "Cores", str(cores))

    # Advanced options
    endpoint = args.endpoint or DEFAULT_ENDPOINT
    if config_input("Configure advanced options? (default: no) ", parse_bool, out):
        endpoint = config_input(
            "Fishnet API endpoint (default: %s): " % (endpoint,), lambda inp: validate_endpoint(inp, endpoint), out
        )

    conf.set("Fishnet", "Endpoint", endpoint)

    # Change key?
    key = None
    if conf.has_option("Fishnet", "Key"):
        if not config_input("Change fishnet key? (default: no) ", parse_bool, out):
            key = conf.get("Fishnet", "Key")

    # Key
    if key is None:
        status = "https://pychess-variants.herokuapp.com" if is_production_endpoint(conf) else "probably not required"
        key = config_input(
            "Personal fishnet key (append ! to force, %s): " % status,
            lambda v: validate_key(v, conf, network=True),
            out,
        )
    conf.set("Fishnet", "Key", key)
    logging.getLogger().addFilter(CensorLogFilter(key))

    # Confirm
    print(file=out)
    while not config_input(
        "Done. Write configuration to %s now? (default: yes) " % (config_file,), lambda v: parse_bool(v, True), out
    ):
        pass

    # Write configuration
    with open(config_file, "w") as f:
        conf.write(f)

    print("Configuration saved.", file=out)
    return conf


def validate_engine_dir(engine_dir):
    if not engine_dir or not engine_dir.strip():
        return os.path.abspath(".")

    engine_dir = os.path.abspath(os.path.expanduser(engine_dir.strip()))

    if not os.path.isdir(engine_dir):
        raise ConfigError("EngineDir not found: %s" % engine_dir)

    return engine_dir


def validate_stockfish_command(stockfish_command, conf):
    if not stockfish_command or not stockfish_command.strip() or stockfish_command.strip().lower() == "download":
        return None

    stockfish_command = stockfish_command.strip()
    engine_dir = get_engine_dir(conf)
    smoke_path = None
    process = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=".fairyfishnet-variant-smoke-",
            suffix=".ini",
            dir=engine_dir,
            delete=False,
        ) as smoke_file:
            smoke_file.write("[fishnet-smoke:chess]\n")
            smoke_path = smoke_file.name

        process = open_process(stockfish_command, engine_dir)
        _, variants = uci(process)
        missing_variants = required_engine_variants.difference(variants)
        if missing_variants:
            raise ConfigError(
                "Ensure you are using pychess custom Fairy-Stockfish. "
                "Unsupported built-in variants: %s" % ", ".join(sorted(missing_variants))
            )

        setoption(process, "VariantPath", os.path.basename(smoke_path))
        _, variants = uci(process)
        if "fishnet-smoke" not in variants:
            raise ConfigError("Fairy-Stockfish does not support loading custom variants.ini files")

        logging.debug("Supported built-in variants: %s", ", ".join(variants))
        return stockfish_command
    finally:
        if process is not None:
            kill_process(process)
        if smoke_path is not None:
            try:
                os.remove(smoke_path)
            except OSError:
                pass


def validate_alice_stockfish_command(alice_stockfish_command, conf):
    """Validate an optional dedicated Alice-Stockfish executable.

    Alice support is an additive capability: an empty command means the worker
    simply does not advertise or accept Alice jobs.
    """

    if not alice_stockfish_command or not alice_stockfish_command.strip():
        return None

    alice_stockfish_command = alice_stockfish_command.strip()
    process = None
    try:
        process = open_process(alice_stockfish_command, get_engine_dir(conf))
        _, variants = uci(process)
        if "alice" not in variants:
            raise ConfigError("Alice-Stockfish does not advertise the alice UCI variant")

        if conf.has_section("AliceStockfish"):
            for name, value in conf.items("AliceStockfish"):
                setoption(process, name, value)
        # Alice-Stockfish is a dedicated single-variant engine. Its only
        # UCI_Variant choice is already the default, and version 1.0 exits if
        # `setoption name UCI_Variant value alice` is sent redundantly.
        isready(process)

        # A successful UCI handshake is not enough for Alice-Stockfish: the
        # configured evaluator/network can still make the first search fail.
        # Probe one shallow search so this worker only advertises Alice after
        # proving it can actually return a move.
        position = current_fen(process)
        if go(process, position, [], depth=1, timeout=5.0).get("bestmove") is None:
            raise ConfigError("Alice-Stockfish search probe did not return a best move")

        return alice_stockfish_command
    except ConfigError:
        raise
    except Exception as err:
        raise ConfigError("Could not start Alice-Stockfish: %s" % err) from err
    finally:
        if process is not None:
            try:
                kill_process(process)
            except OSError:
                pass


def parse_bool(inp, default=False):
    if not inp:
        return default

    inp = inp.strip().lower()
    if not inp:
        return default

    if inp in ["y", "j", "yes", "yep", "true", "t", "1", "ok"]:
        return True
    elif inp in ["n", "no", "nop", "nope", "f", "false", "0"]:
        return False
    else:
        raise ConfigError("Not a boolean value: %s", inp)


def update_nnue():
    try:
        manifest_response = requests.get(NNUE_CATALOGUE_MANIFEST, timeout=HTTP_TIMEOUT)
    except requests.exceptions.RequestException as err:
        raise ConfigError("Could not download NNUE catalogue manifest: %s" % err) from err
    try:
        manifest_response.raise_for_status()
        manifest = response_json(manifest_response, "NNUE catalogue manifest")
    except requests.exceptions.RequestException as err:
        raise ConfigError("Could not download NNUE catalogue manifest: %s" % err) from err
    finally:
        manifest_response.close()
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or not isinstance(manifest.get("networks"), list):
        raise ConfigError("NNUE catalogue manifest has an unsupported format")

    networks = {}
    for entry in manifest["networks"]:
        if not isinstance(entry, dict):
            raise ConfigError("NNUE catalogue manifest contains an invalid network entry")
        network_id = entry.get("id")
        if not isinstance(network_id, str) or not NNUE_ID_RE.fullmatch(network_id):
            raise ConfigError("NNUE catalogue manifest contains an invalid network id")
        if network_id not in nnue_variants and network_id != "nn":
            continue

        filename = entry.get("file")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        url = entry.get("url")
        expected_name = "%s-%s.nnue" % (network_id, digest[:12]) if isinstance(digest, str) else None
        if (
            not isinstance(filename, str)
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or filename != expected_name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or url != NNUE_CATALOGUE_RELEASE_PREFIX + filename
        ):
            raise ConfigError("NNUE catalogue manifest contains an invalid entry for %s" % network_id)
        if network_id in networks:
            raise ConfigError("NNUE catalogue manifest contains duplicate network %s" % network_id)
        networks[network_id] = entry

    if "nn" not in networks:
        raise ConfigError("NNUE catalogue does not contain the standard chess network")

    NNUE_NET.clear()
    for variant in sorted(nnue_variants | {"nn"}):
        entry = networks.get(variant)
        if entry is None:
            continue
        eval_file = entry["file"]
        NNUE_NET[variant] = entry["sha256"][:12]
        if os.path.isfile(eval_file) and os.path.getsize(eval_file) == entry["bytes"]:
            print("%s OK" % eval_file)
            continue

        print("%s downloading from catalogue" % eval_file)
        try:
            download_response = requests.get(entry["url"], timeout=HTTP_TIMEOUT, stream=True)
        except requests.exceptions.RequestException as err:
            raise ConfigError("Could not download NNUE network %s: %s" % (eval_file, err)) from err
        try:
            download_response.raise_for_status()
        except requests.exceptions.RequestException as err:
            download_response.close()
            raise ConfigError("Could not download NNUE network %s: %s" % (eval_file, err)) from err

        digest = hashlib.sha256()
        downloaded = 0
        temp_name = None
        try:
            try:
                with tempfile.NamedTemporaryFile(dir=".", prefix=".%s." % eval_file, delete=False) as fd:
                    temp_name = fd.name
                    with tqdm(
                        total=entry["bytes"],
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=eval_file,
                        disable=not sys.stderr.isatty(),
                    ) as progress:
                        for chunk in download_response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            digest.update(chunk)
                            fd.write(chunk)
                            progress.update(len(chunk))
            except requests.exceptions.RequestException as err:
                raise ConfigError("Could not download NNUE network %s: %s" % (eval_file, err)) from err
            if downloaded != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                raise ConfigError("Downloaded NNUE network failed size or SHA-256 verification: %s" % eval_file)
            os.replace(temp_name, eval_file)
            temp_name = None
        finally:
            download_response.close()
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass


def validate_nnue():
    update_nnue()

    nnue_link = "https://github.com/ianfab/Fairy-Stockfish/wiki/List-of-networks"
    for variant in NNUE_NET:
        nnue_file = "%s-%s.nnue" % (variant, NNUE_NET[variant])
        if not os.path.isfile(nnue_file):
            raise ConfigError("Missing nnue file: %s\nDownload it from %s" % (nnue_file, nnue_link))


def validate_cores(cores):
    if not cores or cores.strip().lower() == "auto":
        return max(1, multiprocessing.cpu_count() - 1)

    if cores.strip().lower() == "all":
        return multiprocessing.cpu_count()

    try:
        cores = int(cores.strip())
    except ValueError:
        raise ConfigError("Number of cores must be an integer")

    if cores < 1:
        raise ConfigError("Need at least one core")

    if cores > multiprocessing.cpu_count():
        raise ConfigError("At most %d cores available on your machine " % multiprocessing.cpu_count())

    return cores


def validate_threads(threads, conf):
    cores = validate_cores(conf_get(conf, "Cores"))

    if not threads or str(threads).strip().lower() == "auto":
        return min(DEFAULT_THREADS, cores)

    try:
        threads = int(str(threads).strip())
    except ValueError:
        raise ConfigError("Number of threads must be an integer")

    if threads < 1:
        raise ConfigError("Need at least one thread per engine process")

    if threads > cores:
        raise ConfigError("%d cores is not enough to run %d threads" % (cores, threads))

    return threads


def validate_memory(memory, conf):
    cores = validate_cores(conf_get(conf, "Cores"))
    threads = validate_threads(conf_get(conf, "Threads"), conf)
    processes = cores // threads

    if not memory or not memory.strip() or memory.strip().lower() == "auto":
        return processes * HASH_DEFAULT

    try:
        memory = int(memory.strip())
    except ValueError:
        raise ConfigError("Memory must be an integer")

    if memory < processes * HASH_MIN:
        raise ConfigError("Not enough memory for a minimum of %d x %d MB in hash tables" % (processes, HASH_MIN))

    if memory > processes * HASH_MAX:
        raise ConfigError(
            "Cannot reasonably use more than %d x %d MB = %d MB for hash tables"
            % (processes, HASH_MAX, processes * HASH_MAX)
        )

    return memory


def validate_endpoint(endpoint, default=DEFAULT_ENDPOINT):
    if not endpoint or not endpoint.strip():
        return default

    if not endpoint.endswith("/"):
        endpoint += "/"

    url_info = urlparse.urlparse(endpoint)
    if url_info.scheme not in ["http", "https"]:
        raise ConfigError("Endpoint does not have http:// or https:// URL scheme")

    return endpoint


def validate_key(key, conf, network=False):
    if not key or not key.strip():
        if is_production_endpoint(conf):
            raise ConfigError("Fishnet key required")
        else:
            return ""

    key = key.strip()

    network = network and not key.endswith("!")
    key = key.rstrip("!").strip()

    if not re.match(r"^[a-zA-Z0-9]+$", key):
        raise ConfigError("Fishnet key is expected to be alphanumeric")

    if network:
        response = requests.get(get_endpoint(conf, "key/%s" % key), timeout=HTTP_TIMEOUT)
        if response.status_code == 404:
            raise ConfigError("Invalid or inactive fishnet key")
        else:
            response.raise_for_status()

    return key


def conf_get(conf, key, default=None, section="Fishnet"):
    if not conf.has_section(section):
        return default
    elif not conf.has_option(section, key):
        return default
    else:
        return conf.get(section, key)


def get_engine_dir(conf):
    return validate_engine_dir(conf_get(conf, "EngineDir"))


def get_stockfish_command(conf, update=True):
    from .downloads import stockfish_filename, update_stockfish

    stockfish_command = validate_stockfish_command(conf_get(conf, "StockfishCommand"), conf)
    if not stockfish_command:
        filename = stockfish_filename()
        if update:
            filename = update_stockfish(conf, filename)
        return validate_stockfish_command(os.path.join(".", filename), conf)
    else:
        return stockfish_command


def get_endpoint(conf, sub=""):
    return urlparse.urljoin(validate_endpoint(conf_get(conf, "Endpoint")), sub)


def is_production_endpoint(conf):
    endpoint = validate_endpoint(conf_get(conf, "Endpoint"))
    hostname = urlparse.urlparse(endpoint).hostname
    return hostname is not None and "pychess" in hostname


def get_key(conf):
    return validate_key(conf_get(conf, "Key"), conf, network=False)


def start_backoff(conf):
    if parse_bool(conf_get(conf, "FixedBackoff")):
        while True:
            yield random.random() * MAX_FIXED_BACKOFF
    else:
        backoff = 1
        while True:
            yield 0.5 * backoff + 0.5 * backoff * random.random()
            backoff = min(backoff + 1, MAX_BACKOFF)
