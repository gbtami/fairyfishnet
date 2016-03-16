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


__version__ = "0.9.0"


class NoJobFound(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason

    def __str__(self):
        return "HTTP %d %s" % (self.status, self.reason)

    def __repr__(self):
        return "HttpError(%d, %s)" % (self.status, self.reason)

class HttpServerError(HttpError):
    pass


def base_url(url):
    url_info = urlparse.urlparse(url)
    return "%s://%s/" % (url_info.scheme, url_info.hostname)


@contextlib.contextmanager
def http_request(method, url, body=None):
    logging.debug("HTTP request: %s %s, body: %s", method, url, body)
    u = urlparse.urlparse(url)
    if u.scheme == "https":
        con = httplib.HTTPSConnection(u.hostname, u.port or 443)
    else:
        con = httplib.HTTPConnection(u.hostname, u.port or 80)
    con.request(method, u.path, body)
    response = con.getresponse()
    logging.debug("HTTP response: %d %s", response.status, response.reason)
    yield response
    con.close()


def available_ram():
    try:
        with open("/proc/meminfo") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    _, ram, unit = line.split()
                    if unit == "kB":
                        return int(ram) // 1024
                    else:
                        logging.error("Unknown unit: %s", unit)
    except IOError:
        return None


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
                    movetime = int(6000000 * 1000 // (nps * self.threads * 0.9 ** (self.threads - 1)))
                    self.conf.set("Fishnet", "Movetime", str(movetime))
                    logging.info("Setting movetime: %d", movetime)
                else:
                    logging.debug("Using movetime: %d", self.conf.getint("Fishnet", "Movetime"))

                # Do the next work unit
                path, request = self.work()

                # Report result and fetch next job
                with http_request("POST", urlparse.urljoin(self.conf.get("Fishnet", "Endpoint"), path), json.dumps(request)) as response:
                    if response.status in [200, 202]:
                        data = response.read().decode("utf-8")
                        logging.debug("Got job: %s", data)

                        self.job = json.loads(data)
                        self.backoff = start_backoff(self.conf)
                    elif response.status == 204:
                        raise NoJobFound()
                    elif 500 <= response.status < 600:
                        raise HttpServerError(response.status, response.reason)
                    else:
                        raise HttpError(response.status, response.reason)
            except NoJobFound:
                self.job = None
                t = next(self.backoff)
                logging.info("No job found. Backing off %0.1fs", t)
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

    # Validate Endpoint
    if not conf.get("Fishnet", "Endpoint").endswith("/"):
        conf.set("Fishnet", "Endpoint", conf.get("Fishnet", "Endpoint") + "/")

    # Log custom UCI options
    for name, value in conf.items("Engine"):
        logging.warn("Using custom UCI option: name %s value %s", name, value)

    # Get number of threads per engine process
    if conf.has_option("Engine", "Threads"):
        threads_per_process = max(conf.getint("Engine", "Threads"), 1)
        conf.remove_option("Engine", "Threads")
    else:
        threads_per_process = 4

    # Hashtable size
    if conf.has_option("Engine", "Hash"):
        memory_per_process = max(conf.getint("Engine", "Hash"), 32)
    else:
        memory_per_process = 256
    logging.info("Hashtable size per process: %d MB", memory_per_process)
    conf.set("Engine", "Hash", str(memory_per_process))

    # Determine the number of spare cores
    total_cores = multiprocessing.cpu_count()
    spare_cores = total_cores - 1
    logging.info("Cores: %d + 1", spare_cores)
    if spare_cores == 0:
        logging.warn("No spare core to exclusively run an engine process")
        spare_cores = 1  # Run 1, anyway

    if conf.has_option("Fishnet", "EngineThreads"):
        if conf.get("Fishnet", "EngineThreads") == "Max":
            spare_processes = total_cores
        elif conf.get("Fishnet", "EngineThreads") == "Auto":
            spare_processes = spare_cores
        else:
            spare_processes = max(conf.getint("Fishnet", "EngineThreads"), 1)
    else:
        spare_processes = spare_cores
    logging.info("Number of engine processes: %d", spare_processes)

    # Determine available memory
    if conf.has_option("Fishnet", "Memory"):
        spare_memory = conf.getint("Fishnet", "Memory")
        logging.info("Memory capped at about %d MB", spare_memory)
    else:
        spare_memory = available_ram()
        if not spare_memory:
            logging.warn("Could not determine available memory, let's hope for the best")
            spare_memory = 256 * spare_cores
        else:
            spare_memory = int(spare_memory * 0.6)
            logging.info("Available memmory: %d MB / 60%%", spare_memory)

    # Let spare cores exclusively run engine processes
    workers = []
    while spare_cores > threads_per_process and spare_processes > 0 and spare_memory > memory_per_process:
        worker = Worker(conf, threads_per_process)
        worker.daemon = True
        workers.append(worker)

        spare_cores -= threads_per_process
        spare_processes -= 1
        spare_memory -= memory_per_process

    # Use the rest of the cores
    if spare_cores > 0 and spare_processes > 0 and spare_memory > memory_per_process:
        worker = Worker(conf, spare_cores)
        worker.daemon = True
        workers.append(worker)

    if not workers:
        logging.error("Not enough resources to start a worker")
        return 1

    # Start all threads and wait forever
    for i, worker in enumerate(workers):
        worker.name = "><> %d" % (i + 1)
        worker.start()
    try:
        while True:
            time.sleep(60)
            logging.info("~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~")
            logging.info("%s", number_to_fishes(sum(worker.positions for worker in workers)))
            logging.info("Analyzed %d positions, crunched %d nodes",
                         sum(worker.positions for worker in workers),
                         sum(worker.nodes for worker in workers))
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
