#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Distributed analysis for lichess.org"""

__version__ = "0.0.1"

import argparse
import logging
import subprocess
import json
import time
import random
import contextlib
import multiprocessing
import threading
import sys

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


class NoJobFound(Exception):
    pass


@contextlib.contextmanager
def http_request(method, url, body=None):
    u = urlparse.urlparse(url)
    if u.scheme == "https":
        con = httplib.HTTPSConnection(u.hostname, u.port or 443)
    else:
        con = httplib.HTTPConnection(u.hostname, u.port or 80)
    con.request(method, u.path, body)
    yield con.getresponse()
    con.close()


def open_process(conf, **kwargs):
    opts = {
        "cwd": conf.get("Fishnet", "EngineDir"),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.PIPE,
        "bufsize": 1,
        "universal_newlines": True,
    }

    opts.update(kwargs)

    return subprocess.Popen(conf.get("Fishnet", "EngineCommand"), **opts)


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
            logging.warn("Unknown command: %s", command)


def isready(p):
    send(p, "isready")
    while True:
        command, _ = recv(p)
        if command == "readyok":
            break
        else:
            logging.warn("Unknown command: %s", command)


def setoption(p, name, value):
    if value is True:
        value = "true"
    elif value is False:
        value = "false"
    elif value is None:
        value = "none"

    send(p, "setoption name %s value %s" % (name, value))


def setoptions(p, conf):
    for name, value in conf.items("Engine"):
        setoption(p, name, value)

    isready(p)


def go(p, conf, starting_fen, uci_moves):
    send(p, "position fen %s moves %s" % (starting_fen, " ".join(uci_moves)))
    isready(p)
    send(p, "go movetime %d" % conf.getint("Fishnet", "Movetime"))

    info = {}
    info["score"] = {}

    while True:
        command, arg = recv(p)

        if command == "bestmove":
            return info
        elif command == "info":
            arg = arg or ""

            # Find multipv parameter first.
            if "multipv" in arg:
                current_parameter = None
                for token in arg.split(" "):
                    if token == "string":
                        break

                    if current_parameter == "multipv":
                        info["multipv"] = token

            # Parse all other parameters.
            current_parameter = None
            score_kind = None
            for token in arg.split(" "):
                if current_parameter == "string":
                    if "string" in info:
                        info["string"] += " " + token
                    else:
                        info["string"] = token
                elif token == "pv":
                    current_parameter = "pv"
                    if info.get("multipv", 1) == 1:
                        info["pv"] = []
                elif token in ["depth", "seldepth", "time", "nodes", "multipv", "score", "currmove", "currmovenumber", "hashfull", "nps", "tbhits", "cpuload", "refutation", "currline", "string"]:
                    current_parameter = token
                elif current_parameter in ["depth", "seldepth", "time", "nodes", "currmovenumber", "hashfull", "nps", "tbhits", "cpuload"]:
                    info[current_parameter] = int(token)
                elif current_parameter == "multipv":
                    # Ignore. Handled before.
                    pass
                elif current_parameter == "score":
                    if token in ["cp", "mate"]:
                        score_kind = token
                    elif token == "lowerbound":
                        info["score"]["lowerbound"] = True
                    elif token == "upperbound":
                        info["score"]["upperbound"] = True
                    elif score_kind:
                        info["score"][score_kind] = int(token)
                elif current_parameter == "pv":
                    if info.get("multipv", 1) == 1:
                        info["pv"].append(token)
                else:
                    if current_parameter in info:
                        info[current_parameter] += " " + token
                    else:
                        info[current_parameter] = token
        else:
            logging.warn("Unknown command: %s", command)


def analyse(p, conf, job):
    variant = job["variant"].lower()
    setoption(p, "UCI_Chess960", variant == "chess960")
    setoption(p, "UCI_KingOfTheHill", variant == "kingofthehill")
    setoption(p, "UCI_3Check", variant == "threecheck")
    setoption(p, "UCI_Horde", variant == "horde")
    isready(p)

    send(p, "ucinewgame")
    isready(p)

    result = []

    for ply in range(len(job["moves"]), -1, -1):
        logging.info("Analysing http://lichess.org/%s#%d" % (job["game_id"], ply))
        part = go(p, conf, job["position"], job["moves"][0:ply])
        result.insert(0, part)

    return result


def quit(p):
    isready(p)

    send(p, "quit")
    time.sleep(1)

    if p.poll() is None:
        logging.warning("Sending SIGTERM to engine process %d" % p.pid)
        p.terminate()
        time.sleep(1)

    if p.poll() is None:
        logging.warning("Sending SIGKILL to engine process %d" % p.pid)
        p.kill()


def bench(conf):
    p = open_process(conf)
    uci(p)
    setoptions(p, conf)

    send(p, "bench")

    while True:
        line = " ".join(recv(p))
        if line.lower().startswith("nodes/second"):
            _, nps = line.split(":")
            return int(nps.strip())

    quit(p)


def main(conf):
    p = open_process(conf)
    engine_info = uci(p)
    logging.info("Started engine process %d: %s" % (p.pid, json.dumps(engine_info)))
    setoptions(p, conf)

    request = {
        "version": __version__,
        "engine": engine_info,
    }

    with http_request("POST", urlparse.urljoin(conf.get("Fishnet", "Endpoint"), "acquire"), json.dumps(request)) as response:
        if response.status == 404:
            raise NoJobFound()

        assert response.status == 200, "HTTP %d" % response.status
        data = response.read().decode("utf-8")
        logging.debug("Got job: %s" % data)
        job = json.loads(data)

    request["analysis"] = analyse(p, conf, job)

    quit(p)

    logging.debug("Sending result: %s", json.dumps(request, indent=2))
    with http_request("POST", urlparse.urljoin(conf.get("Fishnet", "Endpoint"), str(job["game_id"])), json.dumps(request)) as response:
        assert 200 <= response.status < 300, "HTTP %d" % response.status


def main_loop(conf):
    # Initial benchmark
    nps = bench(conf)
    logging.info("Benchmark determined nodes/second: %d", nps)
    if not conf.has_option("Fishnet", "Movetime"):
        movetime = int(3000000 * 1000 / nps)
        conf.set("Fishnet", "Movetime", str(movetime))
        logging.info("Setting movetime: %d", movetime)
    else:
        logging.info("Using movetime: %d", conf.getint("Fishnet", "Movetime"))

    backoff = 1 + random.random()

    # Continuously request and run jobs
    while True:
        try:
            main(conf)
            backoff = 1 + random.random()
        except NoJobFound:
            t = 0.5 * backoff + 0.5 * backoff * random.random()
            logging.warn("No job found. Backing off %0.1fs", t)
            time.sleep(t)
            backoff = min(600, backoff * 2)
        except:
            t = 0.8 * backoff + 0.2 * backoff * random.random()
            logging.exception("Backing off %0.1fs after exception in main loop", t)
            time.sleep(t)
            backoff = min(600, backoff * 2)

def intro():
    print("""
  _____ _     _     _   _      _
 |  ___(_)___| |__ | \ | | ___| |_
 | |_  | / __| '_ \|  \| |/ _ \ __|
 |  _| | \__ \ | | | |\  |  __/ |_
 |_|   |_|___/_| |_|_| \_|\___|\__| %s
 Distributed Stockfish analysis for lichess.org

""" % __version__)

if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conf", type=argparse.FileType("r"), nargs="+")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        format="%(levelname)s:%(name)s:%(threadName)s:%(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO)

    # Parse polyglot.ini
    conf = configparser.SafeConfigParser()
    for c in args.conf:
        conf.readfp(c, c.name)

    # Get number of threads per engine process
    if conf.has_option("Engine", "Threads"):
        threads_per_process = max(conf.getint("Engine", "Threads"), 1)
    else:
        threads_per_process = 1

    intro()

    # Determine number of engine processes to start
    num_processes = (multiprocessing.cpu_count() - 1) // threads_per_process
    if conf.has_option("Fishnet", "Processes"):
        num_processes = min(conf.getint("Fishnet", "Processes"), num_processes)
    num_processes = max(num_processes, 1)
    logging.info("Using %d engine processes on %d cores", num_processes, multiprocessing.cpu_count())

    # Start engine processes
    threads = []
    for _ in range(num_processes):
        thread = threading.Thread(target=main_loop, args=[conf])
        thread.daemon = True
        thread.start()
        threads.append(thread)

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        sys.exit(0)
