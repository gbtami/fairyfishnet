#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Distributed Fairy-Stockfish analysis for pychess-variants"""

from __future__ import print_function
from __future__ import division

import argparse
import logging
import json
import time
import random
import collections
import contextlib
import multiprocessing
import threading
import site
import struct
import sys
import os
import stat
import platform
import re
import textwrap
import getpass
import signal
import ctypes
import string

from bs4 import BeautifulSoup
import gdown

try:
    import requests
except ImportError:
    print("fishnet requires the 'requests' module.", file=sys.stderr)
    print("Try 'pip install requests' or install python-requests from your distro packages.", file=sys.stderr)
    print(file=sys.stderr)
    raise

if os.name == "posix" and sys.version_info[0] < 3:
    try:
        import subprocess32 as subprocess
    except ImportError:
        import subprocess
else:
    import subprocess

try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

try:
    import queue
except ImportError:
    import Queue as queue

try:
    from shlex import quote as shell_quote
except ImportError:
    from pipes import quote as shell_quote

try:
    # Python 2
    input = raw_input
except NameError:
    pass

try:
    import pyffish as sf
    sf_ok = True
    try:
        sf.set_option("VariantPath", "variants.ini")
    except Exception:
        print("No variants.ini found.", file=sys.stderr)
        raise

    try:
        print(sf.version())
    except Exception:
        print("fairyfishnet requires pyffish", file=sys.stderr)
        raise

except ImportError:
    print("No pyffish module installed!", file=sys.stderr)
    sf_ok = False
    raise

try:
    # Python 3
    DEAD_ENGINE_ERRORS = (EOFError, IOError, BrokenPipeError)
except NameError:
    # Python 2
    DEAD_ENGINE_ERRORS = (EOFError, IOError)


__version__ = "1.16.49"

__author__ = "Bajusz Tamás"
__email__ = "gbtami@gmail.com"
__license__ = "GPLv3+"

DEFAULT_ENDPOINT = "https://pychess-variants.herokuapp.com/fishnet/"
STOCKFISH_RELEASES = "https://api.github.com/repos/gbtami/Fairy-Stockfish/releases/latest"
DEFAULT_THREADS = 3
HASH_MIN = 16
HASH_DEFAULT = 256
HASH_MAX = 512
MAX_BACKOFF = 30.0
MAX_FIXED_BACKOFF = 3.0
HTTP_TIMEOUT = 15.0
STAT_INTERVAL = 60.0
DEFAULT_CONFIG = "fishnet.ini"
PROGRESS_REPORT_INTERVAL = 5.0
CHECK_PYPI_CHANCE = 0.01
LVL_SKILL = [-4, 0, 3, 6, 10, 14, 16, 18, 20]
LVL_MOVETIMES = [50, 50, 100, 150, 200, 300, 400, 500, 1000]
LVL_DEPTHS = [1, 1, 1, 2, 3, 5, 8, 13, 22]

NNUE_NET = {}

NNUE_ALIAS = {
    "cambodian": "makruk",
    "chess": "nn",
    "placement": "nn",
}

required_variants = set([
    "ataxx",
    "chess",
    "crazyhouse",
    "placement",
    "atomic",
    "makruk",
    "makpong",
    "cambodian",
    "sittuyin",
    "asean",
    "shogi",
    "minishogi",
    "kyotoshogi",
    "dobutsu",
    "gorogoroplus",
    "torishogi",
    "cannonshogi",
    "xiangqi",
    "manchu",
    "janggi",
    "minixiangqi",
    "capablanca",
    "capahouse",
    "seirawan",
    "shouse",
    "grand",
    "grandhouse",
    "shogun",
    "shako",
    "hoppelpoppel",
    "orda",
    "synochess",
    "shinobi",
    "shinobiplus",
    "empire",
    "ordamirror",
    "chak",
    "chennis",
    "duck",
    "spartan",
    "kingofthehill",
    "3check",
    "mansindam",
    "dragon",
    "khans",
    "antichess",
    "racingkings",
    "horde",
    "shatranj",
])


def intro():
    return r"""
.   _________         .    .
.  (..       \_    ,  |\  /|
.   \       O  \  /|  \ \/ /
.    \______    \/ |   \  /      _____ _     _     _   _      _
.       vvvv\    \ |   /  |     |  ___(_)___| |__ | \ | | ___| |_
.       \^^^^  ==   \_/   |     | |_  | / __| '_ \|  \| |/ _ \ __|
.        `\_   ===    \.  |     |  _| | \__ \ | | | |\  |  __/ |_
.        / /\_   \ /      |     |_|   |_|___/_| |_|_| \_|\___|\__| %s
.        |/   \_  \|      /
.               \________/      Distributed Fairy-Stockfish analysis for pychess-variants
""".lstrip() % __version__


PROGRESS = 15
ENGINE = 5
logging.addLevelName(PROGRESS, "PROGRESS")
logging.addLevelName(ENGINE, "ENGINE")


class LogFormatter(logging.Formatter):
    def format(self, record):
        # Format message
        msg = super(LogFormatter, self).format(record)

        # Add level name
        if record.levelno in [logging.INFO, PROGRESS]:
            with_level = msg
        else:
            with_level = "%s: %s" % (record.levelname, msg)

        # Add thread name
        if record.threadName == "MainThread":
            return with_level
        else:
            return "%s: %s" % (record.threadName, with_level)


class CollapsingLogHandler(logging.StreamHandler):
    def __init__(self, stream=sys.stdout):
        super(CollapsingLogHandler, self).__init__(stream)
        self.last_level = logging.INFO
        self.last_len = 0

    def emit(self, record):
        try:
            if self.last_level == PROGRESS:
                if record.levelno == PROGRESS:
                    self.stream.write("\r")
                else:
                    self.stream.write("\n")

            msg = self.format(record)
            if record.levelno == PROGRESS:
                self.stream.write(msg.ljust(self.last_len))
                self.last_len = max(len(msg), self.last_len)
            else:
                self.last_len = 0
                self.stream.write(msg)
                self.stream.write("\n")

            self.last_level = record.levelno
            self.flush()
        except Exception:
            self.handleError(record)


class TailLogHandler(logging.Handler):
    def __init__(self, capacity, max_level, flush_level, target_handler):
        super(TailLogHandler, self).__init__()
        self.buffer = collections.deque(maxlen=capacity)
        self.max_level = max_level
        self.flush_level = flush_level
        self.target_handler = target_handler

    def emit(self, record):
        if record.levelno < self.max_level:
            self.buffer.append(record)

        if record.levelno >= self.flush_level:
            while self.buffer:
                record = self.buffer.popleft()
                self.target_handler.handle(record)


class CensorLogFilter(logging.Filter):
    def __init__(self, keyword):
        self.keyword = keyword

    def censor(self, msg):
        try:
            # Python 2
            if not isinstance(msg, basestring):
                return msg
        except NameError:
            # Python 3
            if not isinstance(msg, str):
                return msg

        if self.keyword:
            return msg.replace(self.keyword, "*" * len(self.keyword))
        else:
            return msg

    def filter(self, record):
        record.msg = self.censor(record.msg)
        record.args = tuple(self.censor(arg) for arg in record.args)
        return True


def setup_logging(verbosity, stream=sys.stdout):
    logger = logging.getLogger()
    logger.setLevel(ENGINE)

    handler = logging.StreamHandler(stream)

    if verbosity >= 3:
        handler.setLevel(ENGINE)
    elif verbosity >= 2:
        handler.setLevel(logging.DEBUG)
    elif verbosity >= 1:
        handler.setLevel(PROGRESS)
    else:
        if stream.isatty():
            handler = CollapsingLogHandler(stream)
            handler.setLevel(PROGRESS)
        else:
            handler.setLevel(logging.INFO)

    if verbosity < 2:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests.packages.urllib3").setLevel(logging.WARNING)

    tail_target = logging.StreamHandler(stream)
    tail_target.setFormatter(LogFormatter())
    logger.addHandler(TailLogHandler(35, handler.level, logging.ERROR, tail_target))

    handler.setFormatter(LogFormatter())
    logger.addHandler(handler)


def base_url(url):
    url_info = urlparse.urlparse(url)
    return "%s://%s/" % (url_info.scheme, url_info.hostname)


class ConfigError(Exception):
    pass


class UpdateRequired(Exception):
    pass


class Shutdown(Exception):
    pass


class ShutdownSoon(Exception):
    pass


class SignalHandler(object):
    def __init__(self):
        self.ignore = False

        signal.signal(signal.SIGTERM, self.handle_term)
        signal.signal(signal.SIGINT, self.handle_int)

        try:
            signal.signal(signal.SIGUSR1, self.handle_usr1)
        except AttributeError:
            # No SIGUSR1 on Windows
            pass

    def handle_int(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise ShutdownSoon()

    def handle_term(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise Shutdown()

    def handle_usr1(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise UpdateRequired()


def open_process(command, cwd=None, shell=True, _popen_lock=threading.Lock()):
    kwargs = {
        "shell": shell,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.PIPE,
        "bufsize": 1,  # Line buffered
        "universal_newlines": True,
    }

    if cwd is not None:
        kwargs["cwd"] = cwd

    # Prevent signal propagation from parent process
    try:
        # Windows
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    except AttributeError:
        # Unix
        kwargs["preexec_fn"] = os.setpgrp

    with _popen_lock:  # Work around Python 2 Popen race condition
        return subprocess.Popen(command, **kwargs)


def kill_process(p):
    try:
        # Windows
        p.send_signal(signal.CTRL_BREAK_EVENT)
    except AttributeError:
        # Unix
        os.killpg(p.pid, signal.SIGKILL)

    p.communicate()


def send(p, line):
    logging.log(ENGINE, "%s << %s", p.pid, line)
    p.stdin.write(line + "\n")
    p.stdin.flush()


def recv(p):
    while True:
        line = p.stdout.readline()
        if line == "":
            raise EOFError()

        line = line.rstrip()

        logging.log(ENGINE, "%s >> %s", p.pid, line)

        if line:
            return line


def recv_uci(p):
    command_and_args = recv(p).split(None, 1)
    if len(command_and_args) == 1:
        return command_and_args[0], ""
    elif len(command_and_args) == 2:
        return command_and_args


def uci(p):
    send(p, "uci")

    engine_info = {}
    variants = set()

    while True:
        command, arg = recv_uci(p)

        if command == "uciok":
            return engine_info, variants
        elif command == "id":
            name_and_value = arg.split(None, 1)
            if len(name_and_value) == 2:
                engine_info[name_and_value[0]] = name_and_value[1]
        elif command == "option":
            if arg.startswith("name UCI_Variant type combo default chess"):
                for variant in arg.split(" ")[6:]:
                    if variant != "var":
                        variants.add(variant)
        elif command == "Fairy-Stockfish" and " by " in arg:
            # Ignore identification line
            pass
        else:
            logging.warning("Unexpected engine response to uci: %s %s", command, arg)


def isready(p):
    send(p, "isready")
    while True:
        command, arg = recv_uci(p)
        if command == "readyok":
            break
        elif command == "info" and arg.startswith("string "):
            pass
        else:
            logging.warning("Unexpected engine response to isready: %s %s", command, arg)


def setoption(p, name, value):
    if value is True:
        value = "true"
    elif value is False:
        value = "false"
    elif value is None:
        value = "none"

    send(p, "setoption name %s value %s" % (name, value))


def go(p, position, moves, movetime=None, clock=None, depth=None, nodes=None, variant=None, chess960=False):
    send(p, "position fen %s moves %s" % (position, " ".join(moves)))

    builder = []
    builder.append("go")
    if movetime is not None:
        builder.append("movetime")
        builder.append(str(movetime))
    if depth is not None:
        builder.append("depth")
        builder.append(str(depth))
    if nodes is not None:
        builder.append("nodes")
        builder.append(str(nodes))
    if clock is not None:
        builder.append("wtime")
        builder.append(str(clock["wtime"] * 10))
        builder.append("btime")
        builder.append(str(clock["btime"] * 10))
        builder.append("winc")
        builder.append(str(clock["inc"] * 1000))
        builder.append("binc")
        builder.append(str(clock["inc"] * 1000))

    send(p, " ".join(builder))

    info = {}
    info["bestmove"] = None

    while True:
        command, arg = recv_uci(p)

        if command == "bestmove":
            bestmove = arg.split()[0]
            if bestmove and bestmove != "(none)":
                info["bestmove"] = bestmove
            return info

        elif command == "info":
            arg = arg or ""

            # Parse all other parameters
            score_kind, score_value, lowerbound, upperbound = None, None, False, False
            current_parameter = None
            for token in arg.split(" "):
                if current_parameter == "string":
                    # Everything until the end of line is a string
                    if "string" in info:
                        info["string"] += " " + token
                    else:
                        info["string"] = token
                elif token == "score":
                    current_parameter = "score"
                elif token == "pv":
                    current_parameter = "pv"
                    if info.get("multipv", 1) == 1:
                        info.pop("pv", None)
                elif token in ["depth", "seldepth", "time", "nodes", "multipv",
                               "currmove", "currmovenumber",
                               "hashfull", "nps", "tbhits", "cpuload",
                               "refutation", "currline", "string"]:
                    current_parameter = token
                    info.pop(current_parameter, None)
                elif current_parameter in ["depth", "seldepth", "time",
                                           "nodes", "currmovenumber",
                                           "hashfull", "nps", "tbhits",
                                           "cpuload", "multipv"]:
                    # Integer parameters
                    info[current_parameter] = int(token)
                elif current_parameter == "score":
                    # Score
                    if token in ["cp", "mate"]:
                        score_kind = token
                        score_value = None
                    elif token == "lowerbound":
                        lowerbound = True
                    elif token == "upperbound":
                        upperbound = True
                    else:
                        score_value = int(token)
                elif current_parameter != "pv" or info.get("multipv", 1) == 1:
                    # Strings
                    if current_parameter in info:
                        info[current_parameter] += " " + token
                    else:
                        info[current_parameter] = token

            # Set score. Prefer scores that are not just a bound
            if score_kind and score_value is not None and (not (lowerbound or upperbound) or "score" not in info or info["score"].get("lowerbound") or info["score"].get("upperbound")):
                info["score"] = {score_kind: score_value}
                if lowerbound:
                    info["score"]["lowerbound"] = lowerbound
                if upperbound:
                    info["score"]["upperbound"] = upperbound
        else:
            logging.warning("Unexpected engine response to go: %s %s", command, arg)


def file_of(piece: str, rank: str) -> int:
    """
    Returns the 0-based file of the specified piece in the rank.
    Returns -1 if the piece is not in the rank.
    """
    pos = rank.find(piece)
    if pos >= 0:
        return sum(int(p) if p.isdigit() else 1 for p in rank[:pos])
    else:
        return -1


def modded_variant(variant: str, chess960: bool, initial_fen: str) -> str:
    """Some variants need to be treated differently by pyffish."""
    if not chess960 and variant in ("capablanca", "capahouse") and initial_fen:
        """
        E-file king in a Capablanca/Capahouse variant.
        The game will be treated as an Embassy game for the purpose of castling.
        The king starts on the e-file if it is on the e-file in the starting rank and can castle.
        """
        parts = initial_fen.split()
        ranks = parts[0].split("/")
        if (
            parts[2] != "-"
            and (("K" in parts[2] or "Q" in parts[2]) and file_of("K", ranks[7]) == 4)
            and (("k" in parts[2] or "q" in parts[2]) and file_of("k", ranks[0]) == 4)
        ):
            return "embassyhouse" if "house" in variant else "embassy"
    return variant


def set_variant_options(p, variant, chess960, nnue):
    variant = variant.lower()

    setoption(p, "UCI_Chess960", chess960)

    if (variant in NNUE_NET or variant in NNUE_ALIAS) and nnue:
        vari = NNUE_ALIAS[variant] if variant in NNUE_ALIAS else variant
        eval_file = "%s-%s.nnue" % (vari, NNUE_NET.get(vari, ""))
        if os.path.isfile(eval_file):
            setoption(p, "EvalFile", eval_file)

    if variant in ["standard", "fromposition", "chess960"]:
        setoption(p, "UCI_Variant", "chess")
    else:
        setoption(p, "UCI_Variant", variant)


class ProgressReporter(threading.Thread):
    def __init__(self, queue_size, conf):
        super(ProgressReporter, self).__init__()
        self.http = requests.Session()
        self.conf = conf

        self.queue = queue.Queue(maxsize=queue_size)
        self._poison_pill = object()

    def send(self, job, result):
        path = "analysis/%s" % job["work"]["id"]
        data = json.dumps(result).encode("utf-8")
        try:
            self.queue.put_nowait((path, data))
        except queue.Full:
            logging.debug("Could not keep up with progress reports. Dropping one.")

    def stop(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put(self._poison_pill)

    def run(self):
        while True:
            item = self.queue.get()
            if item == self._poison_pill:
                return

            path, data = item

            try:
                response = self.http.post(get_endpoint(self.conf, path),
                                          data=data,
                                          timeout=HTTP_TIMEOUT)
                if response.status_code == 429:
                    logging.error("Too many requests. Suspending progress reports for 60s ...")
                    time.sleep(60.0)
                elif response.status_code != 204:
                    logging.error("Expected status 204 for progress report, got %d", response.status_code)
            except requests.RequestException as err:
                logging.warning("Could not send progress report (%s). Continuing.", err)


class Worker(threading.Thread):
    def __init__(self, conf, threads, memory, progress_reporter):
        super(Worker, self).__init__()
        self.conf = conf
        self.threads = threads
        self.memory = memory

        self.progress_reporter = progress_reporter

        self.alive = True
        self.fatal_error = None
        self.finished = threading.Event()
        self.sleep = threading.Event()
        self.status_lock = threading.RLock()

        self.nodes = 0
        self.positions = 0

        self.stockfish_lock = threading.RLock()
        self.stockfish = None
        self.stockfish_info = None

        self.job = None
        self.backoff = start_backoff(self.conf)

        self.http = requests.Session()
        self.http.mount("http://", requests.adapters.HTTPAdapter(max_retries=1))
        self.http.mount("https://", requests.adapters.HTTPAdapter(max_retries=1))

    def set_name(self, name):
        self.name = name
        self.progress_reporter.name = "%s (P)" % (name, )

    def stop(self):
        with self.status_lock:
            self.alive = False
            self.kill_stockfish()
            self.sleep.set()

    def stop_soon(self):
        with self.status_lock:
            self.alive = False
            self.sleep.set()

    def is_alive(self):
        with self.status_lock:
            return self.alive

    def run(self):
        try:
            while self.is_alive():
                self.run_inner()
        except UpdateRequired as error:
            self.fatal_error = error
        except Exception as error:
            self.fatal_error = error
            logging.exception("Fatal error in worker")
        finally:
            self.finished.set()

    def run_inner(self):
        try:
            # Check if the engine is still alive and start, if necessary
            self.start_stockfish()

            # Do the next work unit
            path, request = self.work()
        except DEAD_ENGINE_ERRORS:
            alive = self.is_alive()
            if alive:
                t = next(self.backoff)
                logging.exception("Engine process has died. Backing off %0.1fs", t)

            # Abort current job
            self.abort_job()

            if alive:
                self.sleep.wait(t)
                self.kill_stockfish()

            return

        try:
            # Report result and fetch next job
            response = self.http.post(get_endpoint(self.conf, path),
                                      json=request,
                                      timeout=HTTP_TIMEOUT)
        except requests.RequestException as err:
            self.job = None
            t = next(self.backoff)
            logging.error("Backing off %0.1fs after failed request (%s)", t, err)
            self.sleep.wait(t)
        else:
            if response.status_code == 204:
                self.job = None
                t = next(self.backoff)
                logging.debug("No job found. Backing off %0.1fs", t)
                self.sleep.wait(t)
            elif response.status_code == 202:
                logging.debug("Got job: %s", response.text)
                self.job = response.json()
                self.backoff = start_backoff(self.conf)
            elif 500 <= response.status_code <= 599:
                self.job = None
                t = next(self.backoff)
                logging.error("Server error: HTTP %d %s. Backing off %0.1fs", response.status_code, response.reason, t)
                self.sleep.wait(t)
            elif 400 <= response.status_code <= 499:
                self.job = None
                t = next(self.backoff) + (60 if response.status_code == 429 else 0)
                try:
                    logging.debug("Client error: HTTP %d %s: %s", response.status_code, response.reason, response.text)
                    error = response.json()["error"]
                    logging.error(error)

                    if "Please restart fishnet to upgrade." in error:
                        logging.error("Stopping worker for update.")
                        raise UpdateRequired()
                except (KeyError, ValueError):
                    logging.error("Client error: HTTP %d %s. Backing off %0.1fs. Request was: %s",
                                  response.status_code, response.reason, t, json.dumps(request))
                self.sleep.wait(t)
            else:
                self.job = None
                t = next(self.backoff)
                logging.error("Unexpected HTTP status for acquire: %d", response.status_code)
                self.sleep.wait(t)

    def abort_job(self):
        if self.job is None:
            return

        logging.debug("Aborting job %s", self.job["work"]["id"])

        try:
            response = requests.post(get_endpoint(self.conf, "abort/%s" % self.job["work"]["id"]),
                                     data=json.dumps(self.make_request()),
                                     timeout=HTTP_TIMEOUT)
            if response.status_code == 204:
                logging.info("Aborted job %s", self.job["work"]["id"])
            else:
                logging.error("Unexpected HTTP status for abort: %d", response.status_code)
        except requests.RequestException:
            logging.exception("Could not abort job. Continuing.")

        self.job = None

    def kill_stockfish(self):
        with self.stockfish_lock:
            if self.stockfish:
                try:
                    kill_process(self.stockfish)
                except OSError:
                    logging.exception("Failed to kill engine process.")
                self.stockfish = None

    def start_stockfish(self):
        with self.stockfish_lock:
            # Check if already running.
            if self.stockfish and self.stockfish.poll() is None:
                return

            # Start process
            self.stockfish = open_process(get_stockfish_command(self.conf, False),
                                          get_engine_dir(self.conf))

        self.stockfish_info, _ = uci(self.stockfish)
        self.stockfish_info.pop("author", None)
        logging.info("Started %s, threads: %s (%d), pid: %d",
                     self.stockfish_info.get("name", "Stockfish <?>"),
                     "+" * self.threads, self.threads, self.stockfish.pid)

        # Prepare UCI options
        self.stockfish_info["options"] = {}
        self.stockfish_info["options"]["threads"] = str(self.threads)
        self.stockfish_info["options"]["hash"] = str(self.memory)

        # Custom options
        if self.conf.has_section("Stockfish"):
            for name, value in self.conf.items("Stockfish"):
                self.stockfish_info["options"][name] = value

        # Add .nnue file list
        self.stockfish_info["nnue"] = ["%s-%s.nnue" % (v, NNUE_NET[v]) for v in NNUE_NET]

        # Set UCI options
        for name, value in self.stockfish_info["options"].items():
            setoption(self.stockfish, name, value)

        isready(self.stockfish)

    def make_request(self):
        return {
            "fishnet": {
                "version": __version__,
                "python": platform.python_version(),
                "apikey": get_key(self.conf),
            },
            "stockfish": self.stockfish_info,
        }

    def work(self):
        result = self.make_request()

        if self.job and self.job["work"]["type"] == "analysis":
            result = self.analysis(self.job)
            return "analysis" + "/" + self.job["work"]["id"], result
        elif self.job and self.job["work"]["type"] == "move":
            result = self.bestmove(self.job)
            return "move" + "/" + self.job["work"]["id"], result
        else:
            if self.job:
                logging.error("Invalid job type: %s", self.job["work"]["type"])

            return "acquire", result

    def job_name(self, job, ply=None):
        builder = []
        if job.get("game_id"):
            builder.append(base_url(get_endpoint(self.conf)))
            builder.append(job["game_id"])
        else:
            builder.append(job["work"]["id"])
        if ply is not None:
            builder.append("#")
            builder.append(str(ply))
        return "".join(builder)

    def bestmove(self, job):
        lvl = job["work"]["level"]
        variant = job.get("variant", "standard")
        chess960 = job.get("chess960", False)
        fen = job["position"]
        moves = job["moves"].split(" ")
        nnue = job.get("nnue", True)

        logging.debug("Playing %s (%s) with lvl %d",
                      self.job_name(job), variant, lvl)

        variant = modded_variant(variant, chess960, fen)
        set_variant_options(self.stockfish, variant, chess960, nnue)
        setoption(self.stockfish, "Skill Level", LVL_SKILL[lvl])
        setoption(self.stockfish, "UCI_AnalyseMode", False)
        send(self.stockfish, "ucinewgame")
        isready(self.stockfish)

        movetime = int(round(LVL_MOVETIMES[lvl] / (self.threads * 0.9 ** (self.threads - 1))))

        start = time.time()
        part = go(self.stockfish, fen, moves,
                  movetime=movetime, clock=job["work"].get("clock"),
                  depth=LVL_DEPTHS[lvl], variant=variant, chess960=chess960)
        end = time.time()

        logging.log(PROGRESS, "Played move in %s (%s) with lvl %d: %0.3fs elapsed, depth %d",
                    self.job_name(job), variant,
                    lvl, end - start, part.get("depth", 0))

        self.nodes += part.get("nodes", 0)
        self.positions += 1

        sfen = False
        show_promoted = variant in (
            "makruk",
            "makpong",
            "cambodian",
            "bughouse",
            "supply",
            "makbug",
        )
        if len(job["moves"]) > 0:
            try:
                fen = sf.get_fen(variant, fen, moves, chess960, sfen, show_promoted)
            except Exception:
                logging.error("sf.get_fen() failed on %s with moves %s", job["position"], job["moves"])

        result = self.make_request()
        result["move"] = {
            "bestmove": part["bestmove"],
            "fen": fen
        }
        return result

    def analysis(self, job):
        variant = job.get("variant", "standard")
        chess960 = job.get("chess960", False)
        fen = job["position"]
        moves = job["moves"].split(" ")
        nnue = job.get("nnue", True)

        result = self.make_request()
        result["analysis"] = [None for _ in range(len(moves) + 1)]
        start = last_progress_report = time.time()

        variant = modded_variant(variant, chess960, fen)
        set_variant_options(self.stockfish, variant, chess960, nnue)
        setoption(self.stockfish, "Skill Level", 20)
        setoption(self.stockfish, "UCI_AnalyseMode", True)
        send(self.stockfish, "ucinewgame")
        isready(self.stockfish)

        nodes = job.get("nodes") or 3500000
        skip = job.get("skipPositions", [])

        num_positions = 0

        for ply in range(len(moves), -1, -1):
            if ply in skip:
                result["analysis"][ply] = {"skipped": True}
                continue

            if last_progress_report + PROGRESS_REPORT_INTERVAL < time.time():
                if self.progress_reporter:
                    self.progress_reporter.send(job, result)
                last_progress_report = time.time()

            logging.log(PROGRESS, "Analysing %s: %s",
                        variant, self.job_name(job, ply))

            part = go(self.stockfish, fen, moves[0:ply],
                      nodes=nodes, movetime=4000, variant=variant, chess960=chess960)

            if "mate" not in part["score"] and "time" in part and part["time"] < 100:
                logging.warning("Very low time reported: %d ms.", part["time"])

            if "nps" in part and part["nps"] >= 100000000:
                logging.warning("Dropping exorbitant nps: %d", part["nps"])
                del part["nps"]

            self.nodes += part.get("nodes", 0)
            self.positions += 1
            num_positions += 1

            result["analysis"][ply] = part

        end = time.time()

        if num_positions:
            logging.info("%s took %0.1fs (%0.2fs per position)",
                         self.job_name(job),
                         end - start, (end - start) / num_positions)
        else:
            logging.info("%s done (nothing to do)", self.job_name(job))

        return result


def detect_cpu_capabilities():
    # Detects support for popcnt and pext instructions
    vendor, modern, bmi2 = "", False, False

    # Run cpuid in subprocess for robustness in case of segfaults
    cmd = []
    cmd.append(sys.executable)
    if __package__ is not None:
        cmd.append("-m")
        cmd.append(os.path.splitext(os.path.basename(__file__))[0])
    else:
        cmd.append(__file__)
    cmd.append("cpuid")

    process = open_process(cmd, shell=False)

    # Parse output
    while True:
        line = process.stdout.readline()
        if not line:
            break

        line = line.rstrip()
        logging.debug("cpuid >> %s", line)
        if not line:
            continue

        columns = line.split()
        if columns[0] == "CPUID":
            pass
        elif len(columns) == 5 and all(all(c in string.hexdigits for c in col) for col in columns):
            eax, a, b, c, d = [int(col, 16) for col in columns]

            # vendor
            if eax == 0:
                vendor = struct.pack("III", b, d, c).decode("utf-8")

            # popcnt
            if eax == 1 and c & (1 << 23):
                modern = True

            # pext
            if eax == 7 and b & (1 << 8):
                bmi2 = True
        else:
            logging.warning("Unexpected cpuid output: %s", line)

    # Done
    process.communicate()
    if process.returncode != 0:
        logging.error("cpuid exited with status code %d", process.returncode)

    return vendor, modern, bmi2


def stockfish_filename():
    machine = platform.machine().lower()

    vendor, modern, bmi2 = detect_cpu_capabilities()
    if modern and "Intel" in vendor and bmi2:
        suffix = "-bmi2"
    elif modern:
        suffix = "-modern"
    else:
        suffix = ""

    if os.name == "nt":
        return "stockfish-windows-%s%s.exe" % (machine, suffix)
    elif os.name == "os2" or sys.platform == "darwin":
        return "stockfish-osx-%s" % machine
    elif os.name == "posix":
        return "stockfish-%s%s" % (machine, suffix)


def download_github_release(conf, release_page, filename):
    path = os.path.join(get_engine_dir(conf), filename)
    logging.info("Engine target path: %s", path)

    headers = {}
    headers["User-Agent"] = "fairyfishnet"

    # Only update to newer versions
    try:
        headers["If-Modified-Since"] = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(os.path.getmtime(path)))
    except OSError:
        pass

    # Escape GitHub API rate limiting
    if "GITHUB_API_TOKEN" in os.environ:
        headers["Authorization"] = "token %s" % os.environ["GITHUB_API_TOKEN"]

    # Find latest release
    logging.info("Looking up %s ...", filename)

    response = requests.get(release_page, headers=headers, timeout=HTTP_TIMEOUT)
    if response.status_code == 304:
        logging.info("Local %s is newer than release", filename)
        return filename
    elif response.status_code != 200:
        raise ConfigError("Failed to look up latest Stockfish release (status %d)" % (response.status_code, ))

    release = response.json()

    logging.info("Latest release is tagged %s", release["tag_name"])

    for asset in release["assets"]:
        if asset["name"] == filename:
            logging.info("Found %s" % asset["browser_download_url"])
            break
    else:
        raise ConfigError("No precompiled %s for your platform" % filename)

    # Download
    logging.info("Downloading %s ...", filename)

    download = requests.get(asset["browser_download_url"], stream=True, timeout=HTTP_TIMEOUT)
    progress = 0
    size = int(download.headers["content-length"])
    with open(path, "wb") as target:
        for chunk in download.iter_content(chunk_size=1024):
            target.write(chunk)
            progress += len(chunk)

            if sys.stderr.isatty():
                sys.stderr.write("\rDownloading %s: %d/%d (%d%%)" % (
                    filename, progress, size,
                    progress * 100 / size))
                sys.stderr.flush()
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()

    # Make executable
    logging.info("chmod +x %s", filename)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC)
    return filename


def update_stockfish(conf, filename):
    return download_github_release(conf, STOCKFISH_RELEASES, filename)


def is_user_site_package():
    try:
        user_site = site.getusersitepackages()
    except AttributeError:
        return False

    return os.path.abspath(__file__).startswith(os.path.join(user_site, ""))


def update_self():
    # Ensure current instance is installed as a package
    if __package__ is None:
        raise ConfigError("Not started as a package (python -m). Cannot update using pip")

    if all(dirname not in ["site-packages", "dist-packages"] for dirname in __file__.split(os.sep)):
        raise ConfigError("Not installed as package (%s). Cannot update using pip" % __file__)

    logging.debug("Package: \"%s\", name: %s, loader: %s",
                  __package__, __name__, __loader__)

    # Ensure pip is available
    try:
        pip_info = subprocess.check_output([sys.executable, "-m", "pip", "--version"],
                                           universal_newlines=True)
    except OSError:
        raise ConfigError("Auto update enabled, but cannot run pip")
    else:
        logging.debug("Pip: %s", pip_info.rstrip())

    # Ensure module file is going to be writable
    try:
        with open(__file__, "r+"):
            pass
    except IOError:
        raise ConfigError("Auto update enabled, but no write permissions "
                          "to module file. Use virtualenv or "
                          "pip install --user")

    # Look up the latest version
    result = requests.get("https://pypi.org/pypi/fairyfishnet/json", timeout=HTTP_TIMEOUT).json()
    latest_version = result["info"]["version"]
    url = result["releases"][latest_version][0]["url"]
    if latest_version == __version__:
        logging.info("Already up to date.")
        return 0

    # Wait
    t = random.random() * 15.0
    logging.info("Waiting %0.1fs before update ...", t)
    time.sleep(t)

    print()

    # Update
    if is_user_site_package():
        logging.info("$ pip install --user --upgrade %s", url)
        ret = subprocess.call([sys.executable, "-m", "pip", "install", "--user", "--upgrade", url],
                              stdout=sys.stdout, stderr=sys.stderr)
    else:
        logging.info("$ pip install --upgrade %s", url)
        ret = subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", url],
                              stdout=sys.stdout, stderr=sys.stderr)
    if ret != 0:
        logging.warning("Unexpected exit code for pip install: %d", ret)
        return ret

    print()

    # Wait
    t = random.random() * 15.0
    logging.info("Waiting %0.1fs before respawn ...", t)
    time.sleep(t)

    # Respawn
    argv = []
    argv.append(sys.executable)
    argv.append("-m")
    argv.append(os.path.splitext(os.path.basename(__file__))[0])
    argv += sys.argv[1:]

    logging.debug("Restarting with execv: %s, argv: %s",
                  sys.executable, " ".join(argv))

    os.execv(sys.executable, argv)


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
    engine_dir = config_input("Engine working directory (default: %s): " % os.path.abspath("."),
                              validate_engine_dir, out)
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
    stockfish_command = config_input("Path or command (will download by default): ",
                                     lambda v: validate_stockfish_command(v, conf),
                                     out)
    if not stockfish_command:
        conf.remove_option("Fishnet", "StockfishCommand")
    else:
        conf.set("Fishnet", "StockfishCommand", stockfish_command)
    print(file=out)

    # Cores
    max_cores = multiprocessing.cpu_count()
    default_cores = max(1, max_cores - 1)
    cores = config_input("Number of cores to use for engine threads (default %d, max %d): " % (default_cores, max_cores),
                         validate_cores, out)
    conf.set("Fishnet", "Cores", str(cores))

    # Advanced options
    endpoint = args.endpoint or DEFAULT_ENDPOINT
    if config_input("Configure advanced options? (default: no) ", parse_bool, out):
        endpoint = config_input("Fishnet API endpoint (default: %s): " % (endpoint, ), lambda inp: validate_endpoint(inp, endpoint), out)

    conf.set("Fishnet", "Endpoint", endpoint)

    # Change key?
    key = None
    if conf.has_option("Fishnet", "Key"):
        if not config_input("Change fishnet key? (default: no) ", parse_bool, out):
            key = conf.get("Fishnet", "Key")

    # Key
    if key is None:
        status = "https://pychess-variants.herokuapp.com" if is_production_endpoint(conf) else "probably not required"
        key = config_input("Personal fishnet key (append ! to force, %s): " % status,
                           lambda v: validate_key(v, conf, network=True), out)
    conf.set("Fishnet", "Key", key)
    logging.getLogger().addFilter(CensorLogFilter(key))

    # Grandhouse is user defined variant
    conf.set("Stockfish", "VariantPath", "variants.ini")

    # Confirm
    print(file=out)
    while not config_input("Done. Write configuration to %s now? (default: yes) " % (config_file, ),
                           lambda v: parse_bool(v, True), out):
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
        raise ConfigError("Ensure you are using pychess custom Fairy-Stockfish. "
                          "Unsupported variants: %s" % ", ".join(missing_variants))

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

    soup = BeautifulSoup(requests.get(url).text, 'html.parser')

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
                href = link.get('href')
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
        with open(eval_file, 'wb') as fd:
            for chunk in download.iter_content(chunk_size=1024):
                fd.write(chunk)
                progress += len(chunk)
                if sys.stderr.isatty():
                    sys.stderr.write("\rDownloading %s: %d/%d (%d%%)" % (
                        eval_file, progress, size,
                        progress * 100 / size))
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
        raise ConfigError("Cannot reasonably use more than %d x %d MB = %d MB for hash tables" % (processes, HASH_MAX, processes * HASH_MAX))

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
    return "pychess" in hostname


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


def update_available():
    try:
        result = requests.get("https://pypi.org/pypi/fairyfishnet/json", timeout=HTTP_TIMEOUT).json()
        latest_version = result["info"]["version"]
    except Exception:
        logging.exception("Failed to check for update on PyPI")
        return False

    if latest_version == __version__:
        logging.info("[fairyfishnet v%s] Client is up to date", __version__)
        return False
    else:
        logging.info("[fairyfishnet v%s] Update available on PyPI: %s",
                     __version__, latest_version)
        return True


def cmd_run(args):
    conf = load_conf(args)

    if args.auto_update:
        print()
        print("### Updating ...")
        print()
        update_self()

    stockfish_command = validate_stockfish_command(conf_get(conf, "StockfishCommand"), conf)
    if not stockfish_command:
        print()
        print("### Updating Stockfish ...")
        print()
        stockfish_command = get_stockfish_command(conf)

    # Check .nnue files
    validate_nnue()

    print()
    print("### Checking configuration ...")
    print()
    print("Python:           %s (with requests %s)" % (platform.python_version(), requests.__version__))
    print("EngineDir:        %s" % get_engine_dir(conf))
    print("StockfishCommand: %s" % stockfish_command)
    print("Key:              %s" % (("*" * len(get_key(conf))) or "(none)"))

    cores = validate_cores(conf_get(conf, "Cores"))
    print("Cores:            %d" % cores)

    threads = validate_threads(conf_get(conf, "Threads"), conf)
    instances = max(1, cores // threads)
    print("Engine processes: %d (each ~%d threads)" % (instances, threads))
    memory = validate_memory(conf_get(conf, "Memory"), conf)
    print("Memory:           %d MB" % memory)
    endpoint = get_endpoint(conf)
    warning = "" if endpoint.startswith("https://") else " (WARNING: not using https)"
    print("Endpoint:         %s%s" % (endpoint, warning))
    print("FixedBackoff:     %s" % parse_bool(conf_get(conf, "FixedBackoff")))
    print()

    if conf.has_section("Stockfish") and conf.items("Stockfish"):
        print("Using custom UCI options is discouraged:")
        for name, value in conf.items("Stockfish"):
            if name.lower() == "hash":
                hint = " (use --memory instead)"
            elif name.lower() == "threads":
                hint = " (use --threads-per-process instead)"
            else:
                hint = ""
            print(" * %s = %s%s" % (name, value, hint))
        print()

    print("### Starting workers ...")
    print()

    buckets = [0] * instances
    for i in range(0, cores):
        buckets[i % instances] += 1

    progress_reporter = ProgressReporter(len(buckets) + 4, conf)
    progress_reporter.daemon = True
    progress_reporter.start()

    workers = [Worker(conf, bucket, memory // instances, progress_reporter) for bucket in buckets]

    # Start all threads
    for i, worker in enumerate(workers):
        worker.set_name("><> %d" % (i + 1))
        worker.daemon = True
        worker.start()

    # Wait while the workers are running
    try:
        # Let SIGTERM and SIGINT gracefully terminate the program
        handler = SignalHandler()

        try:
            while True:
                # Check worker status
                for _ in range(int(max(1, STAT_INTERVAL / len(workers)))):
                    for worker in workers:
                        worker.finished.wait(1.0)
                        if worker.fatal_error:
                            raise worker.fatal_error

                # Log stats
                logging.info("[fishnet v%s] Analyzed %d positions, crunched %d million nodes",
                             __version__,
                             sum(worker.positions for worker in workers),
                             int(sum(worker.nodes for worker in workers) / 1000 / 1000))

                # Check for update
                if random.random() <= CHECK_PYPI_CHANCE and update_available() and args.auto_update:
                    raise UpdateRequired()
        except ShutdownSoon:
            handler = SignalHandler()

            if any(worker.job for worker in workers):
                logging.info("\n\n### Stopping soon. Press ^C again to abort pending jobs ...\n")

            for worker in workers:
                worker.stop_soon()

            for worker in workers:
                while not worker.finished.wait(0.5):
                    pass
    except (Shutdown, ShutdownSoon):
        if any(worker.job for worker in workers):
            logging.info("\n\n### Good bye! Aborting pending jobs ...\n")
        else:
            logging.info("\n\n### Good bye!")
    except UpdateRequired:
        if any(worker.job for worker in workers):
            logging.info("\n\n### Update required! Aborting pending jobs ...\n")
        else:
            logging.info("\n\n### Update required!")
        raise
    finally:
        handler.ignore = True

        # Stop workers
        for worker in workers:
            worker.stop()

        progress_reporter.stop()

        # Wait
        for worker in workers:
            worker.finished.wait()

    return 0


def cmd_configure(args):
    configure(args)
    return 0


def cmd_systemd(args):
    conf = load_conf(args)

    template = textwrap.dedent("""\
        [Unit]
        Description=Fishnet instance
        After=network-online.target
        Wants=network-online.target

        [Service]
        ExecStart={start}
        WorkingDirectory={cwd}
        ReadWriteDirectories={cwd}
        User={user}
        Group={group}
        Nice=5
        CapabilityBoundingSet=
        PrivateTmp=true
        PrivateDevices=true
        DevicePolicy=closed
        ProtectSystem={protect_system}
        NoNewPrivileges=true
        Restart=always

        [Install]
        WantedBy=multi-user.target""")

    # Prepare command line arguments
    builder = [shell_quote(sys.executable)]

    if __package__ is None:
        builder.append(shell_quote(os.path.abspath(sys.argv[0])))
    else:
        builder.append("-m")
        builder.append(shell_quote(os.path.splitext(os.path.basename(__file__))[0]))

    if args.no_conf:
        builder.append("--no-conf")
    else:
        config_file = os.path.abspath(args.conf or DEFAULT_CONFIG)
        builder.append("--conf")
        builder.append(shell_quote(config_file))

    if args.key is not None:
        builder.append("--key")
        builder.append(shell_quote(validate_key(args.key, conf)))
    if args.engine_dir is not None:
        builder.append("--engine-dir")
        builder.append(shell_quote(validate_engine_dir(args.engine_dir)))
    if args.stockfish_command is not None:
        builder.append("--stockfish-command")
        builder.append(shell_quote(validate_stockfish_command(args.stockfish_command, conf)))
    if args.cores is not None:
        builder.append("--cores")
        builder.append(shell_quote(str(validate_cores(args.cores))))
    if args.memory is not None:
        builder.append("--memory")
        builder.append(shell_quote(str(validate_memory(args.memory, conf))))
    if args.threads is not None:
        builder.append("--threads-per-process")
        builder.append(shell_quote(str(validate_threads(args.threads, conf))))
    if args.endpoint is not None:
        builder.append("--endpoint")
        builder.append(shell_quote(validate_endpoint(args.endpoint)))
    if args.fixed_backoff is not None:
        builder.append("--fixed-backoff" if args.fixed_backoff else "--no-fixed-backoff")
    for option_name, option_value in args.setoption:
        builder.append("--setoption")
        builder.append(shell_quote(option_name))
        builder.append(shell_quote(option_value))
    if args.auto_update:
        builder.append("--auto-update")

    builder.append("run")

    start = " ".join(builder)

    protect_system = "full"
    if args.auto_update and os.path.realpath(os.path.abspath(__file__)).startswith("/usr/"):
        protect_system = "false"

    print(template.format(
        user=getpass.getuser(),
        group=getpass.getuser(),
        cwd=os.path.abspath("."),
        start=start,
        protect_system=protect_system
    ))

    try:
        if os.geteuid() == 0:
            print("\n# WARNING: Running as root is not recommended!", file=sys.stderr)
    except AttributeError:
        # No os.getuid() on Windows
        pass

    if sys.stdout.isatty():
        print("\n# Example usage:", file=sys.stderr)
        print("# python -m fishnet systemd | sudo tee /etc/systemd/system/fishnet.service", file=sys.stderr)
        print("# sudo systemctl enable fishnet.service", file=sys.stderr)
        print("# sudo systemctl start fishnet.service", file=sys.stderr)
        print("#", file=sys.stderr)
        print("# Live view of the log: sudo journalctl --follow -u fishnet", file=sys.stderr)


@contextlib.contextmanager
def make_cpuid():
    # Loosely based on cpuid.py by Anders Høst, licensed MIT:
    # https://github.com/flababah/cpuid.py

    # Prepare system information
    is_windows = os.name == "nt"
    is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
    if platform.machine().lower() not in ["amd64", "x86_64", "x86", "i686"]:
        raise OSError("Got no CPUID opcodes for %s" % platform.machine())

    # Struct for return value
    class CPUID_struct(ctypes.Structure):
        _fields_ = [("eax", ctypes.c_uint32),
                    ("ebx", ctypes.c_uint32),
                    ("ecx", ctypes.c_uint32),
                    ("edx", ctypes.c_uint32)]

    # Select kernel32 or libc
    if is_windows:
        libc = ctypes.windll.kernel32
    else:
        libc = ctypes.cdll.LoadLibrary(None)

    # Select opcodes
    if is_64bit:
        if is_windows:
            # Windows x86_64
            # Three first call registers : RCX, RDX, R8
            # Volatile registers         : RAX, RCX, RDX, R8-11
            opc = [
                0x53,                    # push   %rbx
                0x89, 0xd0,              # mov    %edx,%eax
                0x49, 0x89, 0xc9,        # mov    %rcx,%r9
                0x44, 0x89, 0xc1,        # mov    %r8d,%ecx
                0x0f, 0xa2,              # cpuid
                0x41, 0x89, 0x01,        # mov    %eax,(%r9)
                0x41, 0x89, 0x59, 0x04,  # mov    %ebx,0x4(%r9)
                0x41, 0x89, 0x49, 0x08,  # mov    %ecx,0x8(%r9)
                0x41, 0x89, 0x51, 0x0c,  # mov    %edx,0xc(%r9)
                0x5b,                    # pop    %rbx
                0xc3                     # retq
            ]
        else:
            # Posix x86_64
            # Three first call registers : RDI, RSI, RDX
            # Volatile registers         : RAX, RCX, RDX, RSI, RDI, R8-11
            opc = [
                0x53,                    # push   %rbx
                0x89, 0xf0,              # mov    %esi,%eax
                0x89, 0xd1,              # mov    %edx,%ecx
                0x0f, 0xa2,              # cpuid
                0x89, 0x07,              # mov    %eax,(%rdi)
                0x89, 0x5f, 0x04,        # mov    %ebx,0x4(%rdi)
                0x89, 0x4f, 0x08,        # mov    %ecx,0x8(%rdi)
                0x89, 0x57, 0x0c,        # mov    %edx,0xc(%rdi)
                0x5b,                    # pop    %rbx
                0xc3                     # retq
            ]
    else:
        # CDECL 32 bit
        # Three first call registers : Stack (%esp)
        # Volatile registers         : EAX, ECX, EDX
        opc = [
            0x53,                    # push   %ebx
            0x57,                    # push   %edi
            0x8b, 0x7c, 0x24, 0x0c,  # mov    0xc(%esp),%edi
            0x8b, 0x44, 0x24, 0x10,  # mov    0x10(%esp),%eax
            0x8b, 0x4c, 0x24, 0x14,  # mov    0x14(%esp),%ecx
            0x0f, 0xa2,              # cpuid
            0x89, 0x07,              # mov    %eax,(%edi)
            0x89, 0x5f, 0x04,        # mov    %ebx,0x4(%edi)
            0x89, 0x4f, 0x08,        # mov    %ecx,0x8(%edi)
            0x89, 0x57, 0x0c,        # mov    %edx,0xc(%edi)
            0x5f,                    # pop    %edi
            0x5b,                    # pop    %ebx
            0xc3                     # ret
        ]

    code_size = len(opc)
    code = (ctypes.c_ubyte * code_size)(*opc)

    if is_windows:
        # Allocate executable memory
        libc.VirtualAlloc.restype = ctypes.c_void_p
        libc.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong]
        addr = libc.VirtualAlloc(None, code_size, 0x1000, 0x40)
        if not addr:
            raise MemoryError("Could not VirtualAlloc RWX memory")
    else:
        # Allocate memory
        libc.valloc.restype = ctypes.c_void_p
        libc.valloc.argtypes = [ctypes.c_size_t]
        addr = libc.valloc(code_size)
        if not addr:
            raise MemoryError("Could not valloc memory")

        # Make executable
        libc.mprotect.restype = ctypes.c_int
        libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        if 0 != libc.mprotect(addr, code_size, 1 | 2 | 4):
            raise OSError("Failed to set RWX using mprotect")

    # Copy code to allocated executable memory. No need to flush instruction
    # cache for CPUID.
    ctypes.memmove(addr, code, code_size)

    # Create and yield callable
    result = CPUID_struct()
    func_type = ctypes.CFUNCTYPE(None, ctypes.POINTER(CPUID_struct), ctypes.c_uint32, ctypes.c_uint32)
    func_ptr = func_type(addr)

    def cpuid(eax, ecx=0):
        func_ptr(result, eax, ecx)
        return result.eax, result.ebx, result.ecx, result.edx

    yield cpuid

    # Free
    if is_windows:
        libc.VirtualFree.restype = ctypes.c_long
        libc.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]
        libc.VirtualFree(addr, 0, 0x8000)
    else:
        libc.free.restype = None
        libc.free.argtypes = [ctypes.c_void_p]
        libc.free(addr)


def cmd_cpuid(argv):
    with make_cpuid() as cpuid:
        headers = ["CPUID", "EAX", "EBX", "ECX", "EDX"]
        print(" ".join(header.ljust(8) for header in headers).rstrip())

        for eax in [0x0, 0x80000000]:
            highest, _, _, _ = cpuid(eax)
            for eax in range(eax, highest + 1):
                a, b, c, d = cpuid(eax)
                print("%08x %08x %08x %08x %08x" % (eax, a, b, c, d))


def create_variants_ini(args):
    conf = load_conf(args)
    engine_dir = get_engine_dir(conf)

    ini_text = textwrap.dedent("""\
# Hybrid variant of Grand-chess and crazyhouse, using Grand-chess as a template
[grandhouse:grand]
startFen = r8r/1nbqkcabn1/pppppppppp/10/10/10/10/PPPPPPPPPP/1NBQKCABN1/R8R[] w - - 0 1
pieceDrops = true
capturesToHand = true

# Hybrid variant of Gothic-chess and crazyhouse, using Capablanca as a template
[gothhouse:capablanca]
startFen = rnbqckabnr/pppppppppp/10/10/10/10/PPPPPPPPPP/RNBQCKABNR[] w KQkq - 0 1
pieceDrops = true
capturesToHand = true

# Hybrid variant of Embassy chess and crazyhouse, using Embassy as a template
[embassyhouse:embassy]
startFen = rnbqkcabnr/pppppppppp/10/10/10/10/PPPPPPPPPP/RNBQKCABNR[] w KQkq - 0 1
pieceDrops = true
capturesToHand = true

[gorogoroplus:gorogoro]
startFen = sgkgs/5/1ppp1/1PPP1/5/SGKGS[LNln] w 0 1
lance = l
shogiKnight = n
promotedPieceType = l:g n:g

[cannonshogi:shogi]
# No Shogi pawn drop restrictions
dropNoDoubled = -
shogiPawnDropMateIllegal = false
# Soldier is Janggi soldier
soldier = p
# Gold Cannon is exactly like Xiangqi cannon
cannon = u
# Silver Cannon moves and captures like Janggi cannon
# Janggi cannons have this EXCEPTION:
# The cannon cannot use another cannon as a screen. Additionally, it can't capture the opponent's cannons.
# This is NOT exists here.
customPiece1 = a:pR
# Copper Cannon is diagonal Xiangqi cannon
customPiece2 = c:mBcpB
# Iron Cannon is diagonal Janggi cannon
customPiece3 = i:pB
# Flying Silver/Gold Cannon
customPiece4 = w:mRpRmFpB2
# Flying Copper/Iron Cannon
customPiece5 = f:mBpBmWpR2
promotedPieceType = u:w a:w c:f i:f p:g
startFen = lnsgkgsnl/1rci1uab1/p1p1p1p1p/9/9/9/P1P1P1P1P/1BAU1ICR1/LNSGKGSNL[-] w 0 1

[shogun:crazyhouse]
startFen = rnb+fkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB+FKBNR[] w KQkq - 0 1
commoner = c
centaur = g
archbishop = a
chancellor = m
fers = f
promotionRegionWhite = *6 *7 *8
promotionRegionBlack = *3 *2 *1
promotionLimit = g:1 a:1 m:1 q:1
promotionPieceTypes = -
promotedPieceType = p:c n:g b:a r:m f:q
mandatoryPawnPromotion = false
firstRankPawnDrops = true
promotionZonePawnDrops = true
whiteDropRegion = *1 *2 *3 *4 *5
blackDropRegion = *4 *5 *6 *7 *8
immobilityIllegal = true

[orda:chess]
centaur = h
knibis = a
kniroo = l
silver = y
promotionPieceTypes = qh
startFen = lhaykahl/8/pppppppp/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[khans:chess]
centaur = h
knibis = a
kniroo = l
customPiece1 = t:mNcK
customPiece2 = s:mfhNcfW
promotionPawnTypesBlack = s
promotionPieceTypesBlack = t
stalemateValue = loss
nMoveRuleTypesBlack = s
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1
startFen = lhatkahl/ssssssss/8/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1

[synochess:pocketknight]
janggiCannon = c
soldier = s
horse = h
fersAlfil = e
commoner = a
startFen = rneakenr/8/1c4c1/1ss2ss1/8/8/PPPPPPPP/RNBQKBNR[ss] w KQ - 0 1
stalemateValue = loss
perpetualCheckIllegal = true
flyingGeneral = true
blackDropRegion = *5
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[shinobi:crazyhouse]
commoner = c
bers = d
archbishop = j
fers = m
shogiKnight = h
lance = l
promotionRegionWhite = *7 *8
promotionRegionBlack = *2 *1
promotionPieceTypes = -
promotedPieceType = p:c m:b h:n l:r
mandatoryPiecePromotion = true
stalemateValue = loss
nFoldRule = 4
perpetualCheckIllegal = true
startFen = rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/LH1CK1HL[LHMMDJ] w kq - 0 1
capturesToHand = false
whiteDropRegion = *1 *2 *3 *4
immobilityIllegal = true
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[shinobiplus:crazyhouse]
commoner = c
bers = d
dragonHorse = f
archbishop = j
fers = m
shogiKnight = h
lance = l
promotionRegionWhite = *7 *8
promotionRegionBlack = *1 *2 *3
promotionPieceTypes = -
promotedPieceType = p:c m:b h:n l:r
mandatoryPiecePromotion = true
stalemateValue = loss
nFoldRule = 4
perpetualCheckIllegal = true
startFen = rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/4K3[JDFCLHM] w kq - 0 1
capturesToHand = false
whiteDropRegion = *1 *2 *3 *4
immobilityIllegal = true
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[ordamirror:chess]
centaur = h
knibis = a
kniroo = l
customPiece1 = f:mQcN
promotionPieceTypes = lhaf
startFen = lhafkahl/8/pppppppp/8/8/PPPPPPPP/8/LHAFKAHL w - - 0 1
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[empire:chess]
customPiece1 = e:mQcN
customPiece2 = c:mQcB
customPiece3 = t:mQcR
customPiece4 = d:mQcK
soldier = s
promotionPieceTypes = q
startFen = rnbqkbnr/pppppppp/8/8/8/PPPSSPPP/8/TECDKCET w kq - 0 1
stalemateValue = loss
nFoldValue = win
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1
flyingGeneral = true

[chak]
maxRank = 9
maxFile = 9
rook = r
knight = v
centaur = j
immobile = o
customPiece1 = s:FvW
customPiece2 = q:pQ
customPiece3 = d:mQ2cQ2
customPiece4 = p:fsmWfceF
customPiece5 = k:WF
customPiece6 = w:FvW
startFen = rvsqkjsvr/4o4/p1p1p1p1p/9/9/9/P1P1P1P1P/4O4/RVSJKQSVR w - - 0 1
mobilityRegionWhiteCustomPiece6 = *5 *6 *7 *8 *9
mobilityRegionWhiteCustomPiece3 = *5 *6 *7 *8 *9
mobilityRegionBlackCustomPiece6 = *1 *2 *3 *4 *5
mobilityRegionBlackCustomPiece3 = *1 *2 *3 *4 *5
promotionRegionWhite = *5 *6 *7 *8 *9
promotionRegionBlack = *5 *4 *3 *2 *1
promotionPieceTypes = -
mandatoryPiecePromotion = true
promotedPieceType = p:w k:d
extinctionValue = loss
extinctionPieceTypes = kd
extinctionPseudoRoyal = true
flagPiece = d
flagRegionWhite = e8
flagRegionBlack = e2
nMoveRule = 50
nFoldRule = 3
nFoldValue = draw
stalemateValue = loss

[chennis]
maxRank = 7
maxFile = 7
mobilityRegionWhiteKing = b1 c1 d1 e1 f1 b2 c2 d2 e2 f2 b3 c3 d3 e3 f3 b4 c4 d4 e4 f4
mobilityRegionBlackKing = b4 c4 d4 e4 f4 b5 c5 d5 e5 f5 b6 c6 d6 e6 f6 b7 c7 d7 e7 f7
customPiece1 = p:fmWfceF
cannon = c
commoner = m
fers = f
soldier = s
king = k
bishop = b
knight = n
rook = r
promotionPieceTypes = -
promotedPieceType = p:r f:c s:b m:n
promotionRegionWhite = *1 *2 *3 *4 *5 *6 *7
promotionRegionBlack = *7 *6 *5 *4 *3 *2 *1
startFen = 1fkm3/1p1s3/7/7/7/3S1P1/3MKF1[] w - 0 1
pieceDrops = true
capturesToHand = true
pieceDemotion = true
mandatoryPiecePromotion = true
dropPromoted = true
castling = false
stalemateValue = loss

# Mansindam (Pantheon tale)
# A variant that combines drop rule and powerful pieces, and there is no draw
[mansindam]
variantTemplate = shogi
pieceToCharTable = PNBR.Q.CMA.++++...++Kpnbr.q.cma.++++...++k
maxFile = 9
maxRank = 9
pocketSize = 8
startFen = rnbakqcnm/9/ppppppppp/9/9/9/PPPPPPPPP/9/MNCQKABNR[] w - - 0 1
pieceDrops = true
capturesToHand = true
shogiPawn = p
knight = n
bishop = b
rook = r
queen = q
archbishop = c
chancellor = m
amazon = a
king = k
commoner = g
centaur = e
dragonHorse = h
bers = t
customPiece1 = i:BNW
customPiece2 = s:RNF
promotionRegionWhite = *7 *8 *9
promotionRegionBlack = *3 *2 *1
mandatoryPiecePromotion = true
doubleStep = false
castling = false
promotedPieceType = p:g n:e b:h r:t c:i m:s
dropNoDoubled = p
stalemateValue = loss
nMoveRule = 0
nFoldValue = win
flagPiece = k
flagRegionWhite = *9
flagRegionBlack = *1
immobilityIllegal = true

[fogofwar:chess]
king = -
commoner = k
castlingKingPiece = k
extinctionValue = loss
extinctionPieceTypes = k

# Hybrid variant of xiangqi and crazyhouse
[xiangqihouse:xiangqi]
startFen = rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR[] w - - 0 1
pieceDrops = true
capturesToHand = true
dropChecks = false
whiteDropRegion = *1 *2 *3 *4 *5
blackDropRegion = *6 *7 *8 *9 *10
mobilityRegionWhiteFers = d1 f1 e2 d3 f3
mobilityRegionBlackFers = d8 f8 e9 d10 f10
mobilityRegionWhiteElephant = c1 g1 a3 e3 i3 c5 g5
mobilityRegionBlackElephant = c6 g6 a8 e8 i8 c10 g10
mobilityRegionWhiteSoldier = a4 a5 c4 c5 e4 e5 g4 g5 i4 i5 *6 *7 *8 *9 *10
mobilityRegionBlackSoldier = *1 *2 *3 *4 *5 a6 a7 c6 c7 e6 e7 g6 g7 i6 i7

# Hybrid variant of makruk and crazyhouse
[makrukhouse:makruk]
startFen = rnsmksnr/8/pppppppp/8/8/PPPPPPPP/8/RNSKMSNR[] w - - 0 1
pieceDrops = true
capturesToHand = true
firstRankPawnDrops = true
promotionZonePawnDrops = true
immobilityIllegal = true

[makbug:makrukhouse]
startFen = rnsmksnr/8/pppppppp/8/8/PPPPPPPP/8/RNSKMSNR[] w - - 0 1
capturesToHand = false
twoBoards = true

### VARIANT CONTEST ###

# WIP
[melonvariant:chess]

# PIECES
#Cannon Pawn
customPiece1 = p:mKpQ2

#Commoner
commoner = g
#Knight
#Elephant
fersAlfil = e
#Machine
customPiece2 = l:WD
#Alibaba
customPiece3 = s:AD
#Kirin
customPiece4 = f:FD
#Phoenix
customPiece5 = h:WA

#Commoner+ (Queen)
#Knight+ (Nightrider)
customPiece6 = c:NN
#Elephant+
customPiece7 = b:BpB
#Machine+
customPiece8 = r:RpR
#Alibaba+
customPiece9 = m:pQ
#Kirin+
customPiece10 = a:BpR
#Phoenix+
customPiece11 = w:RpB

#Cannon King
customPiece12 = k:KmpQ2

# ROYALTY
extinctionPieceTypes = k
extinctionPseudoRoyal = true
mobilityRegionWhiteCustomPiece12 = c* d* e* f*
mobilityRegionBlackCustomPiece12 = c* d* e* f*

# CAMPMATE
flagPiece = k
flagRegionWhite = *7 *8
flagRegionBlack = *1 *2
flagPieceSafe = true

# PROMOTIONS
promotionRegionWhite = c3 d3 e3 f3 c4 d4 e4 f4 c5 d5 e5 f5 c6 d6 e6 f6
promotionRegionBlack = c3 d3 e3 f3 c4 d4 e4 f4 c5 d5 e5 f5 c6 d6 e6 f6
promotedPieceType = q:g c:n b:e r:l m:s a:f w:h
mandatoryPiecePromotion = true

# DROPS
pieceDrops = true
capturesToHand = true
whiteDropRegion = *1 *2 *7 *8 a* b* g* h*
blackDropRegion = *1 *2 *7 *8 a* b* g* h*

# OTHER RULES
perpetualCheckIllegal = true
nFoldValue = loss
startFen = +r+c+bk+q+a+m+w/pppppppp/8/8/8/8/PPPPPPPP/+W+M+A+QK+B+C+R[] w - 0 1

# MARTIAL ARTS XIANGQI
# V3 of my variant
[xiangfu]

# Board Parameters
maxFile = 9
maxRank = 9

# Pieces

commoner = k
bishop = b
horse = n
rook = r
customPiece2 = e:nAnD
cannon = c
customPiece1 = a:mBcpB

startFen = 2bre4/2can4/2k1k4/9/9/9/4K1K2/4NAC2/4ERB2[] w - 0 1

# Palace
mobilityRegionBlackCommoner = c3 c4 c5 c6 c7 d3 d4 d5 d6 d7 e3 e4 e5 e6 e7 f3 f4 f5 f6 f7 g3 g4 g5 g6 g7
mobilityRegionWhiteCommoner = c3 c4 c5 c6 c7 d3 d4 d5 d6 d7 e3 e4 e5 e6 e7 f3 f4 f5 f6 f7 g3 g4 g5 g6 g7
mobilityRegionBlackElephant = a1 e1 i1 c3 g3 a5 e5 i5 c7 g7 a9 e9 i9
mobilityRegionWhiteElephant = a1 e1 i1 c3 g3 a5 e5 i5 c7 g7 a9 e9 i9

# Drop Rules
pieceDrops = true
capturesToHand = true
whiteDropRegion = *1 *2
blackDropRegion = *8 *9

# Royal piece rules
extinctionPieceTypes = k
extinctionPseudoRoyal = true
dupleCheck = true

# Misc Rules
nMoveRule = 0
perpetualCheckIllegal = true
chasingRule = axf
stalemateValue = loss

[sinting:chess]
customPiece1 = r:vWnD
customPiece2 = n:N
customPiece3 = b:nAfF
customPiece4 = k:fKlW
customPiece5 = q:fKrW
customPiece6 = i:N
startFen = rnbkqbir/pppppppp/8/8/8/8/PPPPPPPP/RIBQKBNR w - - 0 1
mobilityRegionWhiteCustomPiece6 = b1  d1  f1  h1  a2  b2  d2  f2  c3  e3  g3  h3  a4  b4  e4  f4  c5  d5  g5  h5  a6  b6  d6  f6  c7  e7  g7  h7  a8  c8  e8  g8
mobilityRegionBlackCustomPiece6 = b1  d1  f1  h1  a2  b2  d2  f2  c3  e3  g3  h3  a4  b4  e4  f4  c5  d5  g5  h5  a6  b6  d6  f6  c7  e7  g7  h7  a8  c8  e8  g8
mobilityRegionWhiteCustomPiece2 = a1  c1  e1  g1  c2  e2  g2  h2  a3  b3  d3  f3  c4  d4  g4  h4  a5  b5  e5  f5  c6  e6  g6  h6  a7  b7  d7  f7  b8  d8  f8  h8
mobilityRegionBlackCustomPiece2 = a1  c1  e1  g1  c2  e2  g2  h2  a3  b3  d3  f3  c4  d4  g4  h4  a5  b5  e5  f5  c6  e6  g6  h6  a7  b7  d7  f7  b8  d8  f8  h8
extinctionPieceTypes = qk
extinctionPseudoRoyal = true
extinctionValue = loss
promotionPieceTypes = qrbk

[borderlands]
maxFile = 9
maxRank = 10
# Non-promoting pieces.
customPiece1 = c:KmNmAmD
customPiece2 = g:KmNmAmD
# Unpromoted pieces.
customPiece3 = a:RmFcpR
customPiece4 = s:BmWcpB
customPiece5 = h:NmB3
customPiece6 = e:ADmR3
customPiece7 = m:FmN
customPiece8 = f:WmAmD
customPiece9 = w:fWfceFifmnD
customPiece10 = l:KNAD
# Promoted pieces.
customPiece11 = b:RFcpR
customPiece12 = d:BWcpB
customPiece13 = i:NFmWmB3
customPiece14 = j:ADWmFmR3
customPiece15 = k:KmN
customPiece16 = n:KmAmD
customPiece17 = o:NADmQ3
customPiece18 = p:KNAD
promotedPieceType = a:b s:d h:i e:j m:k f:n w:o l:p
pieceValueMg = c:882 g:616 a:1635 b:2501 s:1079 d:1383 h:613 i:1118 e:602 j:1023 m:183 k:428 f:256 n:712 w:284 o:1914 l:1174 p:2680
mandatoryPiecePromotion = true
startFen = a1hs1sh1a/1ce1l1ec1/fwgw1wgwf/w1w1w1w1w/9/9/W1W1W1W1W/FWGW1WGWF/1CE1L1EC1/A1HS1SH1A[MMmm] w - - 0 1
mobilityRegionWhiteCustomPiece1 = *1 *2 *3 *8 *9 *10 a* e* i*
mobilityRegionBlackCustomPiece1 = *1 *2 *3 *8 *9 *10 a* e* i*
mobilityRegionWhiteCustomPiece10 = *1 *2 *3 *4 *5 d7 f7 e9
mobilityRegionBlackCustomPiece10 = *6 *7 *8 *9 *10 d4 f4 e2
flagPiece = *
flagPieceCount = 4
flagRegion = b2 h2 b9 h9
flagMove = true
pieceDrops = true
capturesToHand = false
whiteDropRegion = *6 *7
blackDropRegion = *4 *5
promotionRegionWhite = *8 *9 *10
promotionRegionBlack = *1 *2 *3
doubleStepRegionWhite = *3
doubleStepRegionBlack = *8
nMoveRule = 100
perpetualCheckIllegal = true
moveRepetitionIllegal = true
extinctionValue = loss
extinctionPseudoRoyal = false
extinctionPieceTypes = c
extinctionPieceCount = 0

[battleofideologies:chess]
customPiece1 = s:cFmW
#black royal z (using pseudoroyal and extinction)
customPiece2 = z:FD
customPiece3 = m:KN
customPiece4 = f:bWAD
customPiece5 = e:cffNcbFfDfFW
customPiece6 = x:bWFD
customPiece7 = j:NJ
archbishop = a
chancellor = c
horse = h
maxRank = 9
maxFile = 9
startFen = mfjezejfm/sssssssss/9/9/9/9/9/PPPPPPPPP/RHBCKABHR[sssss] w - - 0 1
nMoveRuleTypesBlack = s
pawnTypes = ps
enPassantTypes = ps

mobilityRegionBlackCustomPiece2 = *9 *8 *7 *6 *5 *2 *1
mobilityRegionBlackCustomPiece3 = *9 *8 *7 *6 *5 *2 *1
mobilityRegionBlackCustomPiece4 = *9 *8 *7 *6 *5 *2 *1
mobilityRegionBlackCustomPiece5 = *9 *8 *7 *6 *5 *2 *1
mobilityRegionBlackCustomPiece7 = *9 *8 *7 *6 *5 *2 *1

mobilityRegionWhiteKing = d1 e1 f1 d2 e2 f2

promotionPawnTypesBlack = s
promotionPieceTypesWhite = hbrac
promotionPieceTypesBlack = jefm
promotionRegionBlack = *5 *4 *3
promotionRegionWhite = *9
#black royal can promote
promotedPieceType = z:x
promotionLimit = s:9
mandatoryPawnPromotion = false
mandatoryPiecePromotion = false
pieceDemotion = true

pieceDrops = true
blackDropRegion = *9 *8 *7 *6

flagPiece = z
flagRegionBlack = *1

extinctionPieceTypes = zx
extinctionValue = loss
extinctionPseudoRoyal = true
dupleCheck = true
stalemateValue = win

[shocking:chess]
connectRegion1Black = d*
connectRegion2Black = e*
connectValue = loss
startFen = dca2acd/moa2aom/ttt2ttt/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1
pawnTypes = pta
enPassantTypes = t
enPassantRegion = *3
promotionPieceTypesBlack = mdo
# missile
customPiece1 = m:RgR
# drone
customPiece2 = d:BgB
# turret
customPiece3 = t:fmWfmpR2fcFfcpB2
# core
customPiece4 = c:KgQ2
extinctionPieceTypes = c
extinctionPseudoRoyal = true
extinctionPieceCount = 1
extinctionOpponentPieceCount = -1
# automaton
customPiece5 = a:fcWfcpR2fmFfmpB2
# rover (better name needed)
customPiece6 = o:FnN

[chess_xiangqi:chess]

maxRank = 9
maxFile = 9
pieceToCharTable = PNBRQaes.w.hc.r............................K
startFen = rheawaehr/9/1c5c1/s1s1s1s1s/9/9/8*/PPPPPPPP*/RNB1KBNR*[Us] w KQ - - 0 1
nMoveRule = 25 #could leave it at 50, doesnt change the balance much, but its just boring
nMoveRuleTypesBlack = s

pawnTypes = ps
enPassantTypes = ps

; soldier = s
; soldierPromotionRank = 5
customPiece1 = s:fR2 # Soldiers have double move,
; customPiece1 = s:fsW
customPiece2 = t:fsW
promotionRegionBlack = *5 *4
promotionPieceTypesBlack = t
# doesnt work
# mandatoryPiecePromotion = true
# promotedPieceType = t:r

flagPiece = t # win the game on promotion
flagRegionBlack = *1

cannon = c
fers = a
horse = h
elephant = e
mobilityRegionBlackElephant = *9 *8 *7 *6 *5 *10

# RED KING
customPiece3 = w:W
mobilityRegionBlackCustomPiece3 = d9 e9 f9 d8 e8 f8 d7 e7 f7
extinctionPieceTypes = wk
extinctionPseudoRoyal = true
mobilityRegionBlackFers = d9 e9 f9 d8 e8 f8 d7 e7 f7


# FOR WHITE
# FOR 9 RANKS
mobilityRegionWhiteKing = *1 *2 *3 *4 *5 *6 a7 b7 c7 g7 h7 i7 a8 b8 c8 g8 h8 i8 a9 b9 c9 g9 h9 i9
promotionRegionWhite = *9
# FOR 10 RANKS
; mobilityRegionBlackFers = d9 e9 f9 d8 e8 f8 d10 e10 f10
; mobilityRegionBlackCustomPiece3 = d9 e9 f9 d8 e8 f8 d10 e10 f10
; mobilityRegionWhiteKing = *1 *2 *3 *4 *5 *6 *7 a8 b8 c8 g8 h8 i8 a9 b9 c9 g9 h9 i9 a10 b10 c10 g10 h10 i10
; promotionRegionWhite = *10

promotionPieceTypesWhite = rnbq

# the queen is a droppable piece
# :mQ means it can move but not capture as a queen
customPiece9 = U:mQ
pieceDrops = true
capturesToHand = false
whiteDropRegion = *1 *2 *3 *4
blackDropRegion = *6 *7 *8 *9

materialCounting = blackdrawodds

# doesnt work when the kings are different
flyingGeneral = true

[variant_000]
#Description: the game is inspired by chess, xiangqi, and shogi (with few elements borrowed from janggi and makruk).
#The game is designed to have slow opening phrase but fast closing phrase with good region control being vital.
#There are 3 main regions in the game for each player (from white perspective): row 1,2,3 are home; row 4,5 are neutral; row 6,7,8 are away.
king = k:K
customPiece1 = q:FWAND
#defensive queen
customPiece2 = b:nAF
#bishop, but ancient
customPiece3 = n:nN
#horse in xiangqi
customPiece4 = r:nDW
#rook but less powerful
customPiece5 = p:fmWfcF
#makruk pawn, but promote differently
customPiece6 = e:BpR
#promoted bishop, with actual bishop move and janggi cannon
customPiece7 = h:NNnZ
#promoted knight, with knightrider move and janggi elephant (including lame block)
customPiece8 = c:RgB
#promoted rook, with actual rook move and a grasshoper bishop (land adjancent square after jump)
customPiece9 = s:WfF
#promoted pawn, a nobleman (silver)
maxRank = 8
maxFile = 8
startFen = rnbqkbnr/8/pppppppp/8/8/PPPPPPPP/8/RNBQKBNR[] w - - 0 1
#game is setup exactly like in makruk with pawns arranged 1 row away from remaining pieces.
mobilityRegionWhiteKing         = d1 d2 e1 e2
mobilityRegionBlackKing         = d8 d7 e8 e7
#king can only move 4 squares of palace inside home region.
mobilityRegionWhiteCustomPiece1 = *1 *2 *3
mobilityRegionBlackCustomPiece1 = *8 *7 *6
#queen can only move inside home region.
promotionRegionWhite = *6 *7 *8
promotionRegionBlack = *3 *2 *1
#similar to makruk and shogi, promotion zone started in the sixth row.
promotedPieceType = b:e n:h r:c p:s
mandatoryPiecePromotion = true
#unlike shogi but like chess or makruk, piece must promote when reaching away zone. as a result, technically no piece promote in last row.
perpetualCheckIllegal = true
#follow xiangqi perpetual check rule
doubleStep = false
castling = false
#procedural set-up
pieceDrops = true
capturesToHand = true
enclosingDrop = ataxx
whiteDropRegion = *4 *5
blackDropRegion = *5 *4
dropNoDoubled = p
dropNoDoubledCount = 0
#captured pieces (not pawn) can be dropped by capturing players; captured promoted pieces are dropped as normal piece (like in shogi).
#however, pieces can only be dropped on neutral zone; also, piece can only be dropped to a square that are adjacent to friendly pieces (ataxx rule).
nFoldValue = loss
#not allow repeating 3 times.
#Tested on fairyground.vercel.app with 101 games of 60000ms+600ms (59300ms for white, tested before time control bug) gives results of 52-0-48 (with 1 timeout).
""")

    ini_file = os.path.join(engine_dir, "variants.ini")
    print(ini_text, file=open(ini_file, "w"))

    sf.set_option("VariantPath", "variants.ini")


def main(argv):
    # Parse command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", default=0, action="count", help="increase verbosity")
    parser.add_argument("--version", action="version", version="fishnet v{0}".format(__version__))

    g = parser.add_argument_group("configuration")
    g.add_argument("--auto-update", action="store_true", help="automatically install available updates")
    g.add_argument("--conf", help="configuration file")
    g.add_argument("--no-conf", action="store_true", help="do not use a configuration file")
    g.add_argument("--key", "--apikey", "-k", help="fishnet api key")

    g = parser.add_argument_group("resources")
    g.add_argument("--cores", help="number of cores to use for engine processes (or auto for n - 1, or all for n)")
    g.add_argument("--memory", help="total memory (MB) to use for engine hashtables")

    g = parser.add_argument_group("advanced")
    g.add_argument("--endpoint", help="pychess-variants http endpoint (default: %s)" % DEFAULT_ENDPOINT)
    g.add_argument("--engine-dir", help="engine working directory")
    g.add_argument("--stockfish-command", help="stockfish command (default: download precompiled Stockfish)")
    g.add_argument("--threads-per-process", "--threads", type=int, dest="threads", help="hint for the number of threads to use per engine process (default: %d)" % DEFAULT_THREADS)
    g.add_argument("--fixed-backoff", action="store_true", default=None, help="fixed backoff (only recommended for move servers)")
    g.add_argument("--no-fixed-backoff", dest="fixed_backoff", action="store_false", default=None)
    g.add_argument("--setoption", "-o", nargs=2, action="append", default=[], metavar=("NAME", "VALUE"), help="set a custom uci option")

    commands = collections.OrderedDict([
        ("run", cmd_run),
        ("configure", cmd_configure),
        ("systemd", cmd_systemd),
        ("cpuid", cmd_cpuid),
    ])

    parser.add_argument("command", default="run", nargs="?", choices=commands.keys())

    args = parser.parse_args(argv[1:])

    # Setup logging
    setup_logging(args.verbose,
                  sys.stderr if args.command == "systemd" else sys.stdout)

    create_variants_ini(args)

    # Show intro
    if args.command not in ["systemd", "cpuid"]:
        print(intro())
        sys.stdout.flush()

    # Run
    try:
        sys.exit(commands[args.command](args))
    except UpdateRequired:
        if args.auto_update:
            logging.info("\n\n### Updating ...\n")
            update_self()

        logging.error("Update required. Exiting (status 70)")
        return 70
    except ConfigError:
        logging.exception("Configuration error")
        return 78
    except (KeyboardInterrupt, Shutdown, ShutdownSoon):
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
