# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Configuration loading, prompting, and validation."""

import configparser
import logging
import multiprocessing
import os
import random
import re
import sys
import urllib.parse as urlparse

import gdown
from bs4 import BeautifulSoup

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
    required_variants,
)
from .dependencies import requests
from .engine import kill_process, open_process, setoption, uci
from .errors import ConfigError
from .logging_utils import CensorLogFilter


def load_conf(args):
    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.add_section("Stockfish")

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

    # Grandhouse is user defined variant
    conf.set("Stockfish", "VariantPath", "variants.ini")

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

    # Ensure the required options are supported
    process = open_process(stockfish_command, engine_dir)
    _, variants = uci(process)

    # Grandhouse is user defined variant
    setoption(process, "VariantPath", "variants.ini")
    _, variants = uci(process)

    kill_process(process)

    logging.debug("Supported variants: %s", ", ".join(variants))

    missing_variants = required_variants.difference(variants)
    if missing_variants:
        raise ConfigError(
            "Ensure you are using pychess custom Fairy-Stockfish. "
            "Unsupported variants: %s" % ", ".join(missing_variants)
        )

    return stockfish_command


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
    url = "https://fairy-stockfish.github.io/nnue/"

    soup = BeautifulSoup(requests.get(url).text, "html.parser")

    # Example link
    # <a href="https://drive.google.com/u/0/uc?id=1r5o5jboZRqND8picxuAbA0VXXMJM1HuS&amp;export=download" rel="nofollow">3check-313cc226a173.nnue</a>
    for link in soup.find_all(href=re.compile("https://drive.google.com/u/0/uc")):
        try:
            parts = link.text.split("-")
            variant, nnue = parts[0], parts[1]
        except IndexError:
            print("Link not supported!")
            print(link)
            continue

        # remove .nnue suffix
        if nnue.endswith(".nnue"):
            nnue = nnue[:-5]
        else:
            continue

        if variant in required_variants:
            NNUE_NET[variant] = nnue

            eval_file = "%s-%s.nnue" % (variant, NNUE_NET[variant])
            if os.path.isfile(eval_file):
                print("%s OK" % eval_file)
            else:
                href = link.get("href")
                if not isinstance(href, str):
                    raise ConfigError("Missing NNUE download URL")
                drive_id = urlparse.parse_qs(urlparse.urlparse(href).query)["id"][0]
                print("%s downloading drive id %s" % (eval_file, drive_id))
                # Adding speed=2000*1024 limit to gdown() may help(?)
                # workers running in the cloud (heroku.com or render.com)
                gdown.download(id=drive_id, output=eval_file, quiet=False)

                if not os.path.isfile(eval_file):
                    print("Failed to download %s" % eval_file)
                    sys.exit(0)

    # Standard chess stockfish nnue
    link = soup.find(href=re.compile("https://tests.stockfishchess.org/api/nn/"))
    if link is None:
        raise ConfigError("Could not find the standard chess NNUE download")
    parts = link.text.split("-")
    variant, nnue = parts[0], parts[1]
    # remove .nnue suffix
    if nnue.endswith(".nnue"):
        nnue = nnue[:-5]
    NNUE_NET["nn"] = nnue

    eval_file = "%s-%s.nnue" % (variant, NNUE_NET[variant])
    if os.path.isfile(eval_file):
        print("%s OK" % eval_file)
    else:
        # href = link.get("href").strip("\\\"")
        href = "https://github.com/official-stockfish/networks/raw/master/%s" % eval_file
        print("%s downloading from %s" % (eval_file, href))
        download = requests.get(href, headers={"User-Agent": "fairyfishnet"}, stream=True)
        progress = 0
        size = 46603 * 1024
        with open(eval_file, "wb") as fd:
            for chunk in download.iter_content(chunk_size=1024):
                fd.write(chunk)
                progress += len(chunk)
                if sys.stderr.isatty():
                    sys.stderr.write(
                        "\rDownloading %s: %d/%d (%d%%)" % (eval_file, progress, size, progress * 100 / size)
                    )
                    sys.stderr.flush()
        if not os.path.isfile(eval_file):
            print("Failed to download %s" % eval_file)
            sys.exit(0)


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
