#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=wrong-import-order

# This file is part of the lichess.org fishnet client.
# Copyright (C) 2016 Niklas Fiekas <niklas.fiekas@backscattering.de>
#
# The MIT license
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is furnished
# to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""Distributed analysis for lichess.org"""


from __future__ import print_function
from __future__ import division


__version__ = "1.3.7"

__author__ = "Niklas Fiekas"
__email__ = "niklas.fiekas@backscattering.de"
__license__ = "MIT"


import argparse
import logging
import json
import time
import random
import contextlib
import multiprocessing
import threading
import sys
import os
import stat
import math
import platform
import re
import textwrap
import getpass
import signal

if os.name == "posix" and sys.version_info[0] < 3:
    try:
        import subprocess32 as subprocess
    except ImportError:
        import subprocess
else:
    import subprocess

try:
    import httplib
except ImportError:
    import http.client as httplib

try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

try:
    import urllib.request as urllib
except ImportError:
    import urllib

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

try:
    from shlex import quote as shell_quote
except ImportError:
    from pipes import quote as shell_quote

try:
    input = raw_input
except NameError:
    pass


DEFAULT_ENDPOINT = "http://en.lichess.org/fishnet/"
DEFAULT_THREADS = 4
HASH_MIN = 32
HASH_DEFAULT = 256
HASH_MAX = 512
DEFAULT_CONFIG = "fishnet.ini"
MAX_BACKOFF = 30.0
MAX_FIXED_BACKOFF = 3.0


def intro():
    return r"""
    _________         .    .
   (..       \_    ,  |\  /|
    \       O  \  /|  \ \/ /
     \______    \/ |   \  /      _____ _     _     _   _      _
        vvvv\    \ |   /  |     |  ___(_)___| |__ | \ | | ___| |_
        \^^^^  ==   \_/   |     | |_  | / __| '_ \|  \| |/ _ \ __|
         `\_   ===    \.  |     |  _| | \__ \ | | | |\  |  __/ |_
         / /\_   \ /      |     |_|   |_|___/_| |_|_| \_|\___|\__| %s
         |/   \_  \|      /
                \________/      Distributed Stockfish analysis for lichess.org
""" % __version__


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


class LogHandler(logging.StreamHandler):
    def __init__(self, collapse_progress=True, stream=sys.stdout):
        super(LogHandler, self).__init__(stream)
        self.last_level = logging.INFO
        self.last_len = 0
        self.collapse_progress = collapse_progress

    def emit(self, record):
        if not self.collapse_progress:
            return super(LogHandler, self).emit(record)

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


def base_url(url):
    url_info = urlparse.urlparse(url)
    return "%s://%s/" % (url_info.scheme, url_info.hostname)


class HttpError(Exception):
    def __init__(self, status, reason, body):
        self.status = status
        self.reason = reason
        self.body = body

    def __str__(self):
        return "HTTP %d %s\n\n%s" % (self.status, self.reason, self.body)

    def __repr__(self):
        return "%s(%d, %r, %r)" % (type(self).__name__, self.status, self.reason, self.body)


class HttpServerError(HttpError):
    pass


class HttpClientError(HttpError):
    pass


class ConfigError(Exception):
    pass


class UpdateRequired(Exception):
    pass


@contextlib.contextmanager
def http(method, url, body=None, headers=None):
    logging.debug("HTTP request: %s %s, body: %s", method, url, body)

    url_info = urlparse.urlparse(url)
    if url_info.scheme == "https":
        con = httplib.HTTPSConnection(url_info.hostname, url_info.port or 443)
    else:
        con = httplib.HTTPConnection(url_info.hostname, url_info.port or 80)

    headers_with_useragent = {"User-Agent": "fishnet %s" % __version__}
    if headers:
        headers_with_useragent.update(headers)

    con.request(method, url_info.path, body, headers_with_useragent)
    response = con.getresponse()
    logging.debug("HTTP response: %d %s", response.status, response.reason)

    try:
        if 400 <= response.status < 500:
            raise HttpClientError(response.status, response.reason, response.read())
        elif 500 <= response.status < 600:
            raise HttpServerError(response.status, response.reason, response.read())
        else:
            yield response
    finally:
        con.close()


def popen_engine(engine_command, engine_dir, _popen_lock=threading.Lock()):
    kwargs = {
        "shell": True,
        "cwd": engine_dir,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.PIPE,
        "bufsize": 1,  # Line buffered
        "universal_newlines": True,
    }

    # Prevent signal progration from parent process
    try:
        # Windows
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    except AttributeError:
        # Unix
        kwargs["preexec_fn"] = os.setpgrp

    with _popen_lock:  # Work around Python 2 Popen race condition
        return subprocess.Popen(engine_command, **kwargs)


def send(p, line):
    logging.log(ENGINE, "%s << %s", p.pid, line)
    p.stdin.write(line)
    p.stdin.write("\n")
    p.stdin.flush()


def recv(p):
    while True:
        line = p.stdout.readline()
        if line == "":
            raise EOFError()
        line = line.rstrip()

        logging.log(ENGINE, "%s >> %s", p.pid, line)

        command_and_args = line.split(None, 1)
        if len(command_and_args) == 1:
            return command_and_args[0], ""
        elif len(command_and_args) == 2:
            return command_and_args


def uci(p):
    send(p, "uci")

    engine_info = {}

    while True:
        command, arg = recv(p)

        if command == "uciok":
            return engine_info
        elif command == "id":
            name_and_value = arg.split(None, 1)
            if len(name_and_value) == 2:
                engine_info[name_and_value[0]] = name_and_value[1]
        elif command == "option":
            pass
        elif command == "Stockfish":
            # Ignore identification line
            pass
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def isready(p):
    send(p, "isready")
    while True:
        command, arg = recv(p)
        if command == "readyok":
            break
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def setoption(p, name, value):
    if value is True:
        value = "true"
    elif value is False:
        value = "false"
    elif value is None:
        value = "none"

    send(p, "setoption name %s value %s" % (name, value))


def go(p, position, moves, movetime=None, depth=None, nodes=None):
    send(p, "position fen %s moves %s" % (position, " ".join(moves)))
    isready(p)

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
    send(p, " ".join(builder))

    info = {}
    info["bestmove"] = None

    while True:
        command, arg = recv(p)

        if command == "bestmove":
            bestmove = arg.split()[0]
            if bestmove and bestmove != "(none)":
                info["bestmove"] = bestmove

            return info
        elif command == "info":
            arg = arg or ""

            # Parse all other parameters
            current_parameter = None
            score_kind = None
            for token in arg.split(" "):
                if current_parameter == "string":
                    # Everything until the end of line is a string
                    if "string" in info:
                        info["string"] += " " + token
                    else:
                        info["string"] = token
                elif token in ["depth", "seldepth", "time", "nodes", "multipv",
                               "score", "currmove", "currmovenumber",
                               "hashfull", "nps", "tbhits", "cpuload",
                               "refutation", "currline", "string", "pv"]:
                    # Next parameter keyword found
                    current_parameter = token
                    if current_parameter != "pv" or info.get("multipv", 1) == 1:
                        info.pop(current_parameter, None)
                elif current_parameter in ["depth", "seldepth", "time",
                                           "nodes", "currmovenumber",
                                           "hashfull", "nps", "tbhits",
                                           "cpuload", "multipv"]:
                    # Integer parameters
                    info[current_parameter] = int(token)
                elif current_parameter == "score":
                    # Score
                    if "score" not in info:
                        info["score"] = {}

                    if token in ["cp", "mate"]:
                        score_kind = token
                    elif token == "lowerbound":
                        info["score"]["lowerbound"] = True
                    elif token == "upperbound":
                        info["score"]["upperbound"] = True
                    elif score_kind:
                        info["score"][score_kind] = int(token)
                elif current_parameter != "pv" or info.get("multipv", 1) == 1:
                    # Strings
                    if current_parameter in info:
                        info[current_parameter] += " " + token
                    else:
                        info[current_parameter] = token

            # Stop immediately in mated positions
            if info["score"].get("mate") == 0 and info.get("multipv", 1) == 1:
                send(p, "stop")
                while True:
                    command, arg = recv(p)
                    if command == "info":
                        logging.info("Ignoring superfluous info: %s", arg)
                    elif command == "bestmove":
                        if not "(none)" in arg:
                            logging.warning("Ignoring bestmove: %s", arg)

                        isready(p)
                        return info
                    else:
                        logging.warning("Unexpected engine output: %s %s", command, arg)
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def set_variant_options(p, variant):
    variant = variant.lower()
    setoption(p, "UCI_Chess960", variant in ["fromposition", "chess960"])
    setoption(p, "UCI_Atomic", variant == "atomic")
    setoption(p, "UCI_Horde", variant == "horde")
    setoption(p, "UCI_House", variant == "crazyhouse")
    setoption(p, "UCI_KingOfTheHill", variant == "kingofthehill")
    setoption(p, "UCI_Race", variant == "racingkings")
    setoption(p, "UCI_3Check", variant == "threecheck")


def depth(level):
    if level in [1, 2]:
        return 1
    elif level == 3:
        return 2
    elif level == 4:
        return 3
    elif level == 5:
        return 5
    elif level == 6:
        return 8
    elif level == 7:
        return 13
    elif level == 8:
        return 21
    else:  # Analysis
        return 99


class Worker(threading.Thread):
    def __init__(self, conf, threads):
        super(Worker, self).__init__()
        self.conf = conf
        self.threads = threads

        self.alive = True
        self.fatal_error = None
        self.finished = threading.Event()
        self.sleep = threading.Event()
        self.status_lock = threading.Lock()

        self.nodes = 0
        self.positions = 0

        self.job = None
        self.process = None
        self.engine_info = None
        self.backoff = start_backoff(self.conf)

    def prepare_stop(self):
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
            # Check if engine is still alive
            if self.process:
                self.process.poll()

            # Restart the engine
            if not self.process or self.process.returncode is not None:
                self.start_engine()

            # Do the next work unit
            path, request = self.work()

            # Report result and fetch next job
            with http("POST", get_endpoint(self.conf, path), json.dumps(request)) as response:
                if response.status == 204:
                    self.job = None
                    t = next(self.backoff)
                    logging.debug("No job found. Backing off %0.1fs", t)
                    self.sleep.wait(t)
                else:
                    data = response.read().decode("utf-8")
                    logging.debug("Got job: %s", data)

                    self.job = json.loads(data)
                    self.backoff = start_backoff(self.conf)
        except HttpServerError as err:
            self.job = None
            t = next(self.backoff)
            logging.error("Server error: HTTP %d %s. Backing off %0.1fs", err.status, err.reason, t)
            self.sleep.wait(t)
        except HttpClientError as err:
            self.job = None
            t = next(self.backoff)
            try:
                logging.debug("Client error: HTTP %d %s: %s", err.status, err.reason, err.body.decode("utf-8"))
                error = json.loads(err.body.decode("utf-8"))["error"]
                logging.error(error)

                if "Please restart fishnet to upgrade." in error:
                    logging.error("Stopping worker for update.")
                    raise UpdateRequired()
            except (KeyError, ValueError):
                logging.error("Client error: HTTP %d %s. Backing off %0.1fs. Request was: %s", err.status, err.reason, t, json.dumps(request))
            self.sleep.wait(t)
        except EOFError:
            if not self.is_alive():
                # Abort
                if self.job:
                    logging.debug("Aborting %s", self.job["work"]["id"])
                    with http("POST", get_endpoint(self.conf, "abort/%s" % self.job["work"]["id"]), json.dumps(self.make_request())) as response:
                        response.read()
                        logging.info("Aborted %s", self.job["work"]["id"])
            else:
                t = next(self.backoff)
                logging.exception("Engine process has died. Backing off %0.1fs", t)
                self.sleep.wait(t)
                self.process.kill()
        except Exception:
            self.job = None
            t = next(self.backoff)
            logging.exception("Backing off %0.1fs after exception in worker", t)
            self.sleep.wait(t)

            # If in doubt, restart engine
            self.process.kill()

    def start_engine(self):
        # Start process
        self.process = popen_engine(get_engine_command(self.conf, False), get_engine_dir(self.conf))
        self.engine_info = uci(self.process)
        logging.info("Started engine process, pid: %d, threads: %d, identification: %s",
                     self.process.pid, self.threads, self.engine_info.get("name", "<none>"))

        # Prepare UCI options
        self.engine_info["options"] = {}
        if self.conf.has_section("Engine"):
            for name, value in self.conf.items("Engine"):
                self.engine_info["options"][name] = value

        self.engine_info["options"]["threads"] = str(self.threads)

        # Set UCI options
        for name, value in self.engine_info["options"].items():
            setoption(self.process, name, value)

        isready(self.process)

    def make_request(self):
        return {
            "fishnet": {
                "version": __version__,
                "python": platform.python_version(),
                "apikey": get_key(self.conf),
            },
            "engine": self.engine_info
        }

    def work(self):
        result = self.make_request()

        if self.job and self.job["work"]["type"] == "analysis":
            result["analysis"] = self.analysis(self.job)
            return "analysis" + "/" + self.job["work"]["id"], result
        elif self.job and self.job["work"]["type"] == "move":
            result["move"] = self.bestmove(self.job)
            return "move" + "/" + self.job["work"]["id"], result
        else:
            if self.job:
                logging.error("Invalid job type: %s", job)

            return "acquire", result

    def bestmove(self, job):
        lvl = job["work"]["level"]
        set_variant_options(self.process, job.get("variant", "standard"))
        setoption(self.process, "Skill Level", int(round((lvl - 1) * 20.0 / 7)))
        isready(self.process)

        moves = job["moves"].split(" ")

        movetime = int(round(4000.0 / (self.threads * 0.9 ** (self.threads - 1)) / 10.0 * lvl / 8.0))

        logging.log(PROGRESS, "Playing %s%s with level %d and movetime %d ms",
                    base_url(get_endpoint(self.conf)), job["game_id"],
                    lvl, movetime)

        part = go(self.process, job["position"], moves,
                  movetime=movetime, depth=depth(lvl))

        self.nodes += part.get("nodes", 0)
        self.positions += 1

        return {
            "bestmove": part["bestmove"],
        }

    def analysis(self, job):
        set_variant_options(self.process, job.get("variant", "standard"))
        setoption(self.process, "Skill Level", 20)
        isready(self.process)

        send(self.process, "ucinewgame")
        isready(self.process)

        moves = job["moves"].split(" ")
        result = []

        start = time.time()

        for ply in range(len(moves), -1, -1):
            logging.log(PROGRESS, "Analysing %s%s#%d",
                        base_url(get_endpoint(self.conf)), job["game_id"], ply)

            part = go(self.process, job["position"], moves[0:ply],
                      nodes=3000000, movetime=4000)

            if "mate" not in part["score"] and "time" in part and part["time"] < 100:
                logging.warning("Very low time reported: %d ms.", part["time"])

            if "nps" in part and part["nps"] >= 100000000:
                logging.warning("Dropping exorbitant nps: %d", part["nps"])
                del part["nps"]

            self.nodes += part.get("nodes", 0)
            self.positions += 1

            result.insert(0, part)

        end = time.time()
        logging.info("%s%s took %0.1fs (%0.2fs per position)",
                     base_url(get_endpoint(self.conf)), job["game_id"],
                     end - start, (end - start) / (len(moves) + 1))

        return result


def stockfish_filename():
    machine = platform.machine().lower()

    if os.name == "posix":
        base = "stockfish-%s" % machine
        with open("/proc/cpuinfo") as cpu_info:
            for line in cpu_info:
                if line.startswith("flags") and "bmi2" in line and "popcnt" in line:
                    return base + "-bmi2"
                if line.startswith("flags") and "popcnt" in line:
                    return base + "-modern"
        return base
    elif os.name == "os2":
        return "stockfish-osx-%s" % machine
    elif os.name == "nt":
        return "stockfish-windows-%s.exe" % machine


def update_stockfish(conf, filename):
    path = os.path.join(get_engine_dir(conf), filename)
    logging.info("Engine target path: %s", path)

    headers = {}

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

    with http("GET", "https://api.github.com/repos/niklasf/Stockfish/releases/latest", headers=headers) as response:
        if response.status == 304:
            logging.info("Local %s is newer than release", filename)
            return filename

        release = json.loads(response.read().decode("utf-8"))

    logging.info("Latest stockfish release is tagged %s", release["tag_name"])

    for asset in release["assets"]:
        if asset["name"] == filename:
            logging.info("Found %s" % asset["browser_download_url"])
            break
    else:
        raise ConfigError("No precompiled %s for your platform" % filename)

    # Download
    logging.info("Downloading %s ...", filename)
    def reporthook(a, b, c):
        sys.stderr.write("\rDownloading %s: %d/%d (%d%%)" % (filename, a * b, c, round(min(a * b, c) * 100 / c)))
        sys.stderr.flush()

    urllib.urlretrieve(asset["browser_download_url"], path, reporthook)

    sys.stderr.write("\n")
    sys.stderr.flush()

    # Make executable
    logging.info("chmod +x %s", filename)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC)
    return filename


def load_conf(args):
    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.add_section("Engine")

    if not args.no_conf:
        if not args.conf and not os.path.isfile(DEFAULT_CONFIG):
            return configure(args)

        config_file = args.conf or DEFAULT_CONFIG
        logging.debug("Using config file: %s", config_file)

        if not conf.read(config_file):
            raise ConfigError("Could not read config file: %s" % config_file)

    if hasattr(args, "engine_dir") and args.engine_dir is not None:
        conf.set("Fishnet", "EngineDir", args.engine_dir)
    if hasattr(args, "engine_command") and args.engine_command is not None:
        conf.set("Fishnet", "EngineCommand", args.engine_command)
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

    return conf


def config_input(prompt=None):
    if prompt:
        sys.stderr.write(prompt)
        sys.stderr.flush()

    return input()


def configure(args):
    print(file=sys.stderr)
    print("### Configuration", file=sys.stderr)
    print(file=sys.stderr)

    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.add_section("Engine")

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
    while True:
        try:
            engine_dir = validate_engine_dir(config_input("Stockfish working directory (default: %s): " % os.path.abspath(".")))
            break
        except ConfigError as error:
            print(error, file=sys.stderr)
    conf.set("Fishnet", "EngineDir", engine_dir)

    # Stockfish command
    print(file=sys.stderr)
    print("Fishnet uses a custom Stockfish build with variant support.", file=sys.stderr)
    print("Stockfish is licensed under the GNU General Public License v3.", file=sys.stderr)
    print("You can find the source at: https://github.com/ddugovic/Stockfish", file=sys.stderr)
    print(file=sys.stderr)
    print("You can build lichess.org custom Stockfish yourself and provide", file=sys.stderr)
    print("the path or automatically download a precompiled binary.", file=sys.stderr)
    print(file=sys.stderr)
    while True:
        try:
            engine_command = validate_engine_command(config_input("Path or command (default: download): "), conf)
            conf.set("Fishnet", "EngineCommand", engine_command or "")

            # Download Stockfish if nescessary
            get_engine_command(conf)

            break
        except ConfigError as error:
            print(error, file=sys.stderr)
    print(file=sys.stderr)

    # Interactive configuration
    while True:
        try:
            max_cores = multiprocessing.cpu_count()
            default_cores = max(1, max_cores - 1)
            cores = validate_cores(config_input("Number of cores to use for engine threads (default %d, max %d): " % (default_cores, max_cores)))
            break
        except ConfigError as error:
            print(error, file=sys.stderr)
    conf.set("Fishnet", "Cores", str(cores))

    while True:
        try:
            default_threads = min(DEFAULT_THREADS, cores)
            threads = validate_threads(config_input("Number of threads to use per engine process (default %d, max %d): "  % (default_threads, cores)), conf)
            break
        except ConfigError as error:
            print(error, file=sys.stderr)
    conf.set("Fishnet", "Threads", str(threads))

    while True:
        try:
            processes = math.ceil(cores / threads)
            min_memory = HASH_MIN * processes
            default_memory = HASH_DEFAULT * processes
            max_memory = HASH_MAX * processes
            memory = validate_memory(config_input("Memory in MB to use for engine hashtables (default %d, min %d, max %d): " % (default_memory, min_memory, max_memory)), conf)
            break
        except ConfigError as error:
            print(error, file=sys.stderr)
    conf.set("Fishnet", "Memory", str(memory))

    while True:
        try:
            advanced = parse_bool(config_input("Configure advanced options? (default: no) "))
            break
        except ConfigError as error:
            print(error, file=sys.stderr)

    endpoint = DEFAULT_ENDPOINT
    fixed_backoff = False
    conf.set("Fishnet", "Endpoint", endpoint)
    conf.set("Fishnet", "FixedBackoff", str(fixed_backoff))

    if advanced:
        while True:
            try:
                endoint = validate_endpoint(config_input("Fishnet API endpoint (default: %s): " % (endpoint, )))
                break
            except ConfigError as error:
                print(error, file=sys.stderr)
        conf.set("Fishnet", "Endpoint", endpoint)

        while True:
            try:
                fixed_backoff = parse_bool(config_input("Fixed backoff? (for move servers, default: no) "))
                break
            except ConfigError as error:
                print(error, file=sys.stderr)
        conf.set("Fishnet", "FixedBackoff", str(fixed_backoff))

    key = None
    if conf.has_option("Fishnet", "Key"):
        while True:
            try:
                change_key = parse_bool(config_input("Change fishnet key? (default: no) "))
                if not change_key:
                    key = conf.get("Fishnet", "Key")
                break
            except ConfigError as error:
                print(error, file=sys.stderr)

    while True:
        try:
            key = validate_key(key or config_input("Personal fishnet key (append ! to force): "), conf, network=True)
            break
        except ConfigError as error:
            print(error, file=sys.stderr)
            key = None
    conf.set("Fishnet", "Key", key)

    print(file=sys.stderr)
    while True:
        try:
            if parse_bool(config_input("Done. Write configuration to %s now? (default: yes) " % (config_file, )), True):
                break
        except ConfigError as error:
            print(error, file=sys.stderr)

    # Write configuration
    with open(config_file, "w") as f:
        conf.write(f)

    print("Configuration saved.", file=sys.stderr)
    return conf


def validate_engine_dir(engine_dir):
    if not engine_dir or not engine_dir.strip():
        return os.path.abspath(".")

    engine_dir = os.path.abspath(os.path.expanduser(engine_dir.strip()))

    if not os.path.isdir(engine_dir):
        raise ConfigError("EngineDir not found: %s" % engine_dir)

    return engine_dir


def validate_engine_command(engine_command, conf):
    if not engine_command or not engine_command.strip() or engine_command.strip().lower() == "download":
        return None

    engine_command = engine_command.strip()
    engine_dir = get_engine_dir(conf)

    # Ensure the required options are supported
    process = popen_engine(engine_command, engine_dir)
    options = []
    send(process, "uci")
    while True:
        command, arg = recv(process)

        if command == "uciok":
            break
        elif command in ["id", "Stockfish"]:
            pass
        elif command == "option":
            name = []
            for token in arg.split(" ")[1:]:
                if name and token == "type":
                    break
                name.append(token)
            options.append(" ".join(name))
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)
    process.kill()

    logging.debug("Supported options: %s", ", ".join(options))

    required_options = ["UCI_Chess960", "UCI_Atomic", "UCI_Horde", "UCI_House",
                        "UCI_KingOfTheHill", "UCI_Race", "UCI_3Check",
                        "Threads", "Hash"]

    for required_option in required_options:
        if required_option not in options:
            raise ConfigError("Unsupported engine option %s. Ensure you are using lichess custom Stockfish" % required_option)

    return engine_command


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
    processes = int(math.ceil(cores / threads))

    if not memory or not memory.strip() or memory.strip().lower() == "auto":
        return processes * HASH_DEFAULT

    try:
        memory = int(memory.strip())
    except ValueError:
        raise ConfigError("Memory must be an integer")

    if memory < processes * HASH_MIN:
        raise ConfigError("Not enough memory for a minimum of %d x %d MB in hash tables" % (processes, HASH_MIN))

    if memory > processes * HASH_MAX:
        raise ConfigError("Can not reasonably use more than %d x %d MB = %d MB for hash tables" % (processes, HASH_MAX, processes * HASH_MAX))

    return memory


def validate_endpoint(endpoint):
    if not endpoint or not endpoint.strip():
        return DEFAULT_ENDPOINT

    if not endpoint.endswith("/"):
        endpoint += "/"

    return endpoint


def validate_key(key, conf, network=False):
    if not key or not key.strip():
        raise ConfigError("Fishnet key required")

    key = key.strip()

    network = network and not key.endswith("!")
    key = key.rstrip("!").strip()

    if not re.match(r"^[a-zA-Z0-9]+$", key):
        raise ConfigError("Fishnet key is expected to be alphanumeric")

    if network:
        try:
            with http("GET", get_endpoint(conf, "key/%s" % key)) as response:
                pass
        except HttpClientError as error:
            if error.status == 404:
                raise ConfigError("Invalid or inactive fishnet key")
            else:
                raise

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


def get_engine_command(conf, update=True):
    engine_command = validate_engine_command(conf_get(conf, "EngineCommand"), conf)
    if not engine_command:
        filename = stockfish_filename()
        if update:
            filename = update_stockfish(conf, filename)
        return validate_engine_command(os.path.join(".", filename), conf)
    else:
        return engine_command


def get_endpoint(conf, sub=""):
    return urlparse.urljoin(validate_endpoint(conf_get(conf, "Endpoint")), sub)


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


def cmd_run(args):
    conf = load_conf(args)

    engine_command = validate_engine_command(conf_get(conf, "EngineCommand"), conf)
    if not engine_command:
        print()
        print("### Updating Stockfish ...")
        print()
        engine_command = get_engine_command(conf)

    print()
    print("### Checking configuration")
    print()
    print("EngineDir:     %s" % get_engine_dir(conf))
    print("EngineCommand: %s" % engine_command)
    print("Key:           %s" % ("*" * len(get_key(conf))))

    spare_threads = validate_cores(conf_get(conf, "Cores"))
    print("Cores:         %d" % spare_threads)
    threads_per_process = validate_threads(conf_get(conf, "Threads"), conf)
    print("Threads:       %d (per engine process)" % threads_per_process)
    memory = validate_memory(conf_get(conf, "Memory"), conf)
    print("Memory:        %d MB" % memory)
    print("Endpoint:      %s" % get_endpoint(conf))
    print("FixedBackoff:  %s" % parse_bool(conf_get(conf, "FixedBackoff")))
    print()

    if conf.has_section("Engine") and conf.items("Engine"):
        print("Using custom UCI options is discouraged:")
        for name, value in conf.items("Engine"):
            print(" * %s = %s" % (name, value))
        print()

    print("### Starting workers ...")
    print()

    # Let spare cores exclusively run engine processes
    workers = []
    while spare_threads > threads_per_process:
        workers.append(Worker(conf, threads_per_process))
        spare_threads -= threads_per_process

    # Use the rest of the cores
    if spare_threads > 0:
        workers.append(Worker(conf, spare_threads))

    # Start all threads and wait forever
    for i, worker in enumerate(workers):
        worker.name = "><> %d" % (i + 1)
        worker.start()
    try:
        while True:
            # Check worker status
            for worker in workers:
                worker.finished.wait(60 / len(workers))
                if worker.fatal_error:
                    raise worker.fatal_error

            # Log stats
            logging.info("[fishnet v%s] Analyzed %d positions, crunched %d million nodes",
                         __version__,
                         sum(worker.positions for worker in workers),
                         int(sum(worker.nodes for worker in workers) / 1000 / 1000))
    except KeyboardInterrupt:
        # Ignore additional keyboard interrupts
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        logging.info("\n\n### Good bye! Aborting pending jobs ...\n")

        # Prepare to stop workers
        for worker in workers:
            worker.prepare_stop()

        # Kill engine processes
        for worker in workers:
            try:
                # Windows
                worker.process.send_signal(signal.CTRL_BREAK_EVENT)
            except AttributeError:
                # Unix
                worker.process.kill()

        # Wait
        for worker in workers:
            worker.finished.wait()

        return 0


def cmd_stockfish(args):
    conf = load_conf(args)

    print()
    print("### Stockfish")
    os.chdir(get_engine_dir(conf))
    return subprocess.call(get_engine_command(conf), shell=True)


def cmd_configure(args):
    configure(args)
    return 0


def cmd_systemd(args):
    conf = load_conf(args)

    template = textwrap.dedent("""\
        [Unit]
        Description=Fishnet instance
        After=network.target

        [Service]
        User={user}
        Group={group}
        WorkingDirectory={cwd}
        Environment=PATH={path}
        ExecStart={start}
        KillSignal=SIGINT
        Restart=always

        [Install]
        WantedBy=multi-user.target""")

    config_file = os.path.abspath(args.conf or DEFAULT_CONFIG)

    # Prepare command line arguments
    builder = [shell_quote(sys.executable), shell_quote(os.path.abspath(sys.argv[0]))]

    if not args.no_conf:
        builder.append("--conf")
        builder.append(shell_quote(os.path.abspath(args.conf or DEFAULT_CONFIG)))
    else:
        builder.append("--no-conf")
        if args.key is not None:
            builder.append("--key")
            builder.append(shell_quote(validate_key(args.key, conf)))
        if args.engine_dir is not None:
            builder.append("--engine-dir")
            builder.append(shell_quote(validate_engine_dir(args.engine_dir)))
        if args.engine_command is not None:
            builder.append("--engine-command")
            builder.append(shell_quote(validate_engine_command(args.engine_command, conf)))
        if args.cores is not None:
            builder.append("--cores")
            builder.append(shell_quote(str(validate_cores(args.cores))))
        if args.memory is not None:
            builder.append("--memory")
            builder.append(shell_quote(str(validate_memory(args.memory, conf))))
        if args.threads is not None:
            builder.append("--threads")
            builder.append(shell_quote(str(validate_threads(args.threads, conf))))
        if args.endpoint is not None:
            builder.append("--endpoint")
            builder.append(shell_quote(validate_endpoint(args.endpoint)))
        if args.fixed_backoff:
            builder.append("--fixed-backoff")
    builder.append("run")

    start = " ".join(builder)

    # Virtualenv support
    if hasattr(sys, "real_prefix"):
        start = "while [ true ]; do %s; ret=$?; if [ $ret -eq 70 ]; then pip download fishnet || sleep 10; pip install --upgrade fishnet || sleep 10; else exit $ret; fi; sleep 5; done" % start
        shell_cmd = "source %s; %s" % (shell_quote(os.path.abspath(os.path.join(sys.prefix, "bin", "activate"))), start)
        start = "/bin/bash -c %s" % shell_quote(shell_cmd)

    print(template.format(
        user=getpass.getuser(),
        group=getpass.getuser(),
        cwd=os.path.abspath("."),
        path=shell_quote(os.environ.get("PATH", "")),
        start=start
    ))

    print(file=sys.stderr)

    if os.geteuid() == 0:
        print("# WARNING: Running as root is not recommended!", file=sys.stderr)
        print(file=sys.stderr)

    if not hasattr(sys, "real_prefix"):
        print("# WARNING: Using a virtualenv (to enable auto update) is recommended!", file=sys.stderr)
        print(file=sys.stderr)

    print("# Example usage:", file=sys.stderr)
    print("# python -m fishnet systemd | sudo tee /etc/systemd/system/fishnet.service", file=sys.stderr)
    print("# sudo systemctl enable fishnet.service", file=sys.stderr)
    print("# sudo systemctl start fishnet.service", file=sys.stderr)


def main(argv):
    # Parse command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", default=0, action="count", help="increase verbosity")
    parser.add_argument("--version", action="version", version="fishnet v{0}".format(__version__))
    parser.add_argument("--conf", help="configuration file")
    parser.add_argument("--no-conf", action="store_true", help="do not use a configuration file")

    parser.add_argument("--key", "--apikey", "-k", help="fishnet api key")

    parser.add_argument("--engine-dir", help="engine working directory")
    parser.add_argument("--engine-command", "-e", help="engine command (default: download precompiled Stockfish)")

    parser.add_argument("--cores", help="number of cores to use for engine processes (or auto for n - 1, or all for n)")
    parser.add_argument("--memory", help="total number of memory (MB) to use for engine hashtables")
    parser.add_argument("--threads", type=int, help="number of threads per engine process (default: 4)")
    parser.add_argument("--endpoint", help="lichess http endpoint")
    parser.add_argument("--fixed-backoff", action="store_true", help="fixed backoff (only recommended for move servers)")

    parser.add_argument("command", default="run", nargs="?", choices=["run", "configure", "systemd", "stockfish"])

    commands = {
        "run": cmd_run,
        "configure": cmd_configure,
        "systemd": cmd_systemd,
        "stockfish": cmd_stockfish,
    }

    args = parser.parse_args(argv[1:])

    # Setup logging
    logger = logging.getLogger()
    collapse_progress = False
    if args.verbose >= 3:
        logger.setLevel(ENGINE)
    elif args.verbose >= 2:
        logger.setLevel(logging.DEBUG)
    elif args.verbose >= 1:
        logger.setLevel(PROGRESS)
    else:
        if sys.stdout.isatty():
            collapse_progress = True
            logger.setLevel(PROGRESS)
        else:
            logger.setLevel(logging.INFO)
    handler = LogHandler(collapse_progress, sys.stderr if args.command == "systemd" else sys.stdout)
    handler.setFormatter(LogFormatter())
    logger.addHandler(handler)

    # Show intro
    if args.command != "systemd":
        print(intro())

    # Run
    try:
        sys.exit(commands[args.command](args))
    except UpdateRequired:
        logging.error("Update required. Exiting (status 70)")
        return 70
    except ConfigError:
        logging.exception("Configuration error")
        return 78


if __name__ == "__main__":
    sys.exit(main(sys.argv))
