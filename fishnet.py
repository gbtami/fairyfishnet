#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=wrong-import-order

"""Distributed analysis for lichess.org"""

from __future__ import print_function

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
import math

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
    import configparser
except ImportError:
    import ConfigParser as configparser


__version__ = "0.9.1"


def base_url(url):
    url_info = urlparse.urlparse(url)
    return "%s://%s/" % (url_info.scheme, url_info.hostname)


class HttpError(Exception):
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason

    def __str__(self):
        return "HTTP %d %s" % (self.status, self.reason)

    def __repr__(self):
        return "%s(%d, %s)" % (type(self).__name__, self.status, self.reason)


class HttpServerError(HttpError):
    pass


class HttpClientError(HttpError):
    pass


@contextlib.contextmanager
def http_request(method, url, body=None):
    logging.debug("HTTP request: %s %s, body: %s", method, url, body)

    url_info = urlparse.urlparse(url)
    if url_info.scheme == "https":
        con = httplib.HTTPSConnection(url_info.hostname, url_info.port or 443)
    else:
        con = httplib.HTTPConnection(url_info.hostname, url_info.port or 80)

    con.request(method, url_info.path, body)
    response = con.getresponse()
    logging.debug("HTTP response: %d %s", response.status, response.reason)

    try:
        if 400 <= response.status < 500:
            raise HttpClientError(response.status, response.reason)
        elif 500 <= response.status < 600:
            raise HttpServerError(response.status, response.reason)
        else:
            yield response
    finally:
        con.close()


class NoJobFound(Exception):
    pass


def start_backoff(conf):
    if conf.has_option("Fishnet", "Fixed Backoff"):
        while True:
            yield random.random() * conf.getfloat("Fishnet", "Fixed Backoff")
    else:
        backoff = 1
        while True:
            yield 0.5 * backoff + 0.5 * backoff * random.random()
            backoff = min(backoff + 1, 60)


def open_process(conf, _popen_lock=threading.Lock()):
    with _popen_lock:  # Work around Python 2 Popen race condition
        return subprocess.Popen(conf.get("Fishnet", "EngineCommand"),
                                shell=True,
                                cwd=conf.get("Fishnet", "EngineDir"),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE,
                                bufsize=1,  # Line buffered
                                universal_newlines=True)


def send(p, line):
    logging.debug("%s << %s", p.pid, line)
    p.stdin.write(line)
    p.stdin.write("\n")
    p.stdin.flush()


def recv(p):
    while True:
        line = p.stdout.readline()
        if line == "":
            raise EOFError()
        line = line.rstrip()

        logging.debug("%s >> %s", p.pid, line)

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
            logging.warn("Unexpected engine output: %s %s", command, arg)


def isready(p):
    send(p, "isready")
    while True:
        command, arg = recv(p)
        if command == "readyok":
            break
        else:
            logging.warn("Unexpected engine output: %s %s", command, arg)


def setoption(p, name, value):
    if value is True:
        value = "true"
    elif value is False:
        value = "false"
    elif value is None:
        value = "none"

    send(p, "setoption name %s value %s" % (name, value))


def movetime(conf, level):
    time = conf.getint("Fishnet", "Movetime")

    if not level:  # Analysis
        return time

    # For play, divide analysis time per 10, then scale to level
    return int(round(time / 10.0 * level / 8.0))


def depth(level):
    if not level:  # Analysis
        return 99
    elif level < 5:
        return level
    elif level == 5:
        return 6
    elif level == 6:
        return 8
    elif level == 7:
        return 10
    else:
        return 99


def go(p, starting_fen, uci_moves, movetime, depth):
    send(p, "position fen %s moves %s" % (starting_fen, " ".join(uci_moves)))
    isready(p)
    send(p, "go movetime %d depth %d" % (movetime, depth))

    info = {}

    while True:
        command, arg = recv(p)

        if command == "bestmove":
            bestmove = arg.split()[0]
            if bestmove and bestmove != "(none)":
                info["bestmove"] = bestmove
            else:
                info["bestmove"] = None

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
        else:
            logging.warn("Unexpected engine output: %s %s", command, arg)


def set_variant_options(p, job):
    variant = job["variant"].lower()
    setoption(p, "UCI_Chess960", variant == "chess960")
    setoption(p, "UCI_Atomic", variant == "atomic")
    setoption(p, "UCI_Horde", variant == "horde")
    setoption(p, "UCI_House", variant == "crazyhouse")
    setoption(p, "UCI_KingOfTheHill", variant == "kingofthehill")
    setoption(p, "UCI_Race", variant == "racingkings")
    setoption(p, "UCI_3Check", variant == "threecheck")


def bench(p):
    send(p, "bench")

    while True:
        line = " ".join(recv(p))
        if line.lower().startswith("nodes/second"):
            _, nps = line.split(":")
            return int(nps.strip())
        elif any(line.lower().startswith(prefix)
                 for prefix in ["info", "position:", "===", "bestmove",
                                "nodes searched", "total time"]):
            pass
        else:
            logging.warn("Unexpected engine output: %s", line)


class Worker(threading.Thread):
    def __init__(self, conf, threads):
        super(Worker, self).__init__()
        self.conf = conf
        self.threads = threads

        self.nodes = 0
        self.positions = 0

        self.job = None
        self.process = None
        self.engine_info = None
        self.backoff = start_backoff(self.conf)

    def run(self):
        while True:
            try:
                # Check if engine is still alive
                if self.process:
                    self.process.poll()

                # Restart the engine
                if not self.process or self.process.returncode is not None:
                    self.start_engine()

                # Determine movetime by benchmark or config
                if not self.conf.has_option("Fishnet", "Movetime"):
                    logging.info("Running benchmark ...")
                    nps = bench(self.process)
                    logging.info("Benchmark determined nodes/second: %d", nps)
                    movetime = int(5000000 * 1000 // (nps * self.threads * 0.9 ** (self.threads - 1)))
                    self.conf.set("Fishnet", "Movetime", str(movetime))
                    logging.info("Setting movetime: %d", movetime)
                else:
                    logging.debug("Using movetime: %d", self.conf.getint("Fishnet", "Movetime"))

                # Do the next work unit
                path, request = self.work()

                # Report result and fetch next job
                with http_request("POST", urlparse.urljoin(self.conf.get("Fishnet", "Endpoint"), path), json.dumps(request)) as response:
                    if response.status == 204:
                        raise NoJobFound()
                    else:
                        data = response.read().decode("utf-8")
                        logging.debug("Got job: %s", data)

                        self.job = json.loads(data)
                        self.backoff = start_backoff(self.conf)
            except NoJobFound:
                self.job = None
                t = next(self.backoff)
                logging.debug("No job found. Backing off %0.1fs", t)
                time.sleep(t)
            except HttpServerError as err:
                self.job = None
                t = next(self.backoff)
                logging.error("Server error: HTTP %d %s. Backing off %0.1fs", err.status, err.reason, t)
                time.sleep(t)
            except:
                self.job = None
                t = next(self.backoff)
                logging.exception("Backing off %0.1fs after exception in worker", t)
                time.sleep(t)

                # If in doubt, restart engine
                self.process.kill()

    def start_engine(self):
        self.process = open_process(self.conf)
        self.engine_info = uci(self.process)
        logging.info("Started engine process, pid: %d, threads: %d, identification: %s",
                     self.process.pid, self.threads, self.engine_info.get("name", "<none>"))

        # Prepare UCI options
        self.engine_info["options"] = {}
        for name, value in self.conf.items("Engine"):
            self.engine_info["options"][name] = value

        self.engine_info["options"]["threads"] = str(self.threads)

        # Set UCI options
        for name, value in self.engine_info["options"].items():
            setoption(self.process, name, value)

        isready(self.process)

    def work(self):
        result = {
            "fishnet": {
                "version": __version__,
                "apikey": self.conf.get("Fishnet", "Apikey"),
            },
            "engine": self.engine_info
        }

        if self.job and self.job["work"]["type"] == "analysis":
            result["analysis"] = self.analyse()
            return "analysis" + "/" + self.job["work"]["id"], result
        elif self.job and self.job["work"]["type"] == "move":
            result["move"] = self.bestmove()
            return "move" + "/" + self.job["work"]["id"], result
        else:
            if self.job:
                logging.error("Invalid job type: %s", job)

            return "acquire", result

    def bestmove(self):
        lvl = self.job["work"]["level"]
        set_variant_options(self.process, self.job)
        setoption(self.process, "Skill Level", int(round((lvl - 1) * 20.0 / 7)))
        isready(self.process)

        send(self.process, "ucinewgame")
        isready(self.process)

        moves = self.job["moves"].split(" ")

        logging.info("Playing %s%s level %s",
                     base_url(self.conf.get("Fishnet", "Endpoint")),
                     self.job["game_id"], self.job["work"]["level"])

        part = go(self.process, self.job["position"], moves,
                  movetime(self.conf, lvl), depth(lvl))

        self.nodes += part.get("nodes", 0)
        self.positions += 1

        return {
            "bestmove": part["bestmove"],
        }

    def analyse(self):
        set_variant_options(self.process, self.job)
        setoption(self.process, "Skill Level", 20)
        isready(self.process)

        send(self.process, "ucinewgame")
        isready(self.process)

        moves = self.job["moves"].split(" ")
        result = []

        for ply in range(len(moves), -1, -1):
            logging.info("Analysing %s%s#%d",
                         base_url(self.conf.get("Fishnet", "Endpoint")),
                         self.job["game_id"], ply)

            part = go(self.process, self.job["position"], moves[0:ply],
                      movetime(self.conf, None), depth(None))

            self.nodes += part.get("nodes", 0)
            self.positions += 1

            result.insert(0, part)

        return result


def number_to_fishes(number):
    swarm = []

    number = min(200000, number)

    while number >= 100000:
        swarm.append("><XXXX'> °")
        number -= 100000

    while number >= 10000:
        swarm.append("<?))>{{")
        number -= 10000

    while number >= 1000:
        swarm.append("><(('>")
        number -= 1000

    while number >= 100:
        swarm.append("<'))><")
        number -= 100

    while number >= 10:
        swarm.append("><('>")
        number -= 10

    while number >= 1:
        swarm.append("<><")
        number -= 1

    random.shuffle(swarm)
    return "  ".join(swarm)


def intro():
    print(r"""
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
""" % __version__)


def main(args):
    # Setup logging
    logging.basicConfig(
        stream=sys.stdout,
        format="%(levelname)s: %(threadName)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO)

    # Parse polyglot.ini
    conf = configparser.SafeConfigParser()
    for c in args.conf:
        conf.readfp(c, c.name)

    # Ensure Apikey is set
    if not conf.has_option("Fishnet", "Apikey"):
        logging.error("Apikey not found. Check configuration")
        return 78

    # Validate EngineDir
    if not os.path.isdir(conf.get("Fishnet", "EngineDir")):
        logging.error("EngineDir not found. Check configuration")
        return 78

    # Sanitize Endpoint
    if not conf.get("Fishnet", "Endpoint").endswith("/"):
        conf.set("Fishnet", "Endpoint", conf.get("Fishnet", "Endpoint") + "/")

    # Log custom UCI options
    for name, value in conf.items("Engine"):
        logging.warn("Using custom UCI option: name %s value %s", name, value)

    # Determine number of cores to use for engine threads
    if not conf.has_option("Fishnet", "Cores") or conf.get("Fishnet", "Cores").lower() == "auto":
        spare_threads = multiprocessing.cpu_count() - 1
    elif conf.get("Fishnet", "Cores").lower() == "all":
        spare_threads = multiprocessing.cpu_count()
    else:
        spare_threads = conf.getint("Fishnet", "Cores")

    if spare_threads == 0:
        logging.warn("Not enough cores to exclusively run an engine thread")
        spare_threads = 1
    elif spare_threads > multiprocessing.cpu_count():
        logging.warn("Using more threads than cores: %d/%d", spare_threads, multiprocessing.cpu_count())
    else:
        logging.info("Using %d cores: %d", spare_threads)

    # Get number of threads per engine process
    if conf.has_option("Engine", "Threads"):
        threads_per_process = max(conf.getint("Engine", "Threads"), 1)
        conf.remove_option("Engine", "Threads")
    else:
        threads_per_process = 4

    # Determine memory to use per process
    if conf.has_option("Engine", "Hash"):
        memory_per_process = conf.getint("Engine", "Hash")
    elif conf.has_option("Fishnet", "Memory"):
        memory_per_process = conf.getint("Fishnet", "Memory") // math.ceil(spare_threads / threads_per_process)
    else:
        memory_per_process = 256

    conf.set("Engine", "Hash", str(memory_per_process))

    if memory_per_process < 32:
        logging.warn("Very small hashtable size per engine process: %d MB", memory_per_process)
    else:
        logging.info("Hashtable size per process: %d MB", memory_per_process)


    # Let spare cores exclusively run engine processes
    workers = []
    while spare_threads > threads_per_process:
        worker = Worker(conf, threads_per_process)
        worker.daemon = True
        workers.append(worker)

        spare_threads -= threads_per_process

    # Use the rest of the cores
    if spare_threads > 0:
        worker = Worker(conf, spare_threads)
        worker.daemon = True
        workers.append(worker)

    # Start all threads and wait forever
    for i, worker in enumerate(workers):
        worker.name = "><> %d" % (i + 1)
        worker.start()
    try:
        while True:
            time.sleep(60)
            logging.info("Analyzed %d positions, crunched %d million nodes  %s",
                         sum(worker.positions for worker in workers),
                         int(sum(worker.nodes for worker in workers) / 1000 / 1000),
                         number_to_fishes(sum(worker.positions for worker in workers)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    intro()

    # Parse command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conf", type=argparse.FileType("r"), nargs="+")
    parser.add_argument("--verbose", "-v", action="store_true")

    # Run
    sys.exit(main(parser.parse_args()))
