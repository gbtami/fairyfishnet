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
import os

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


def open_process(conf):
    return subprocess.Popen(conf.get("Fishnet", "EngineCommand"),
                            cwd=conf.get("Fishnet", "EngineDir"),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            stdin=subprocess.PIPE,
                            bufsize=1,
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
            logging.warn("Unknown command: %s %s", command, arg)


def isready(p):
    send(p, "isready")
    while True:
        command, arg = recv(p)
        if command == "readyok":
            break
        else:
            logging.warn("Unknown command: %s %s", command, arg)


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


def go(p, conf, starting_fen, uci_moves, collect_infos):
    send(p, "position fen %s moves %s" % (starting_fen, " ".join(uci_moves)))
    isready(p)
    send(p, "go movetime %d" % conf.getint("Fishnet", "Movetime"))

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
        elif command == "info" and not collect_infos:
            continue
        elif command == "info":
            arg = arg or ""

            # Find multipv parameter first
            if "multipv" in arg:
                current_parameter = None
                for token in arg.split(" "):
                    if token == "string":
                        break

                    if current_parameter == "multipv":
                        info["multipv"] = token

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
                               "refutation", "currline", "string"]:
                    # Next parameter keyword found
                    current_parameter = token
                    if current_parameter != "pv" or info.get("multipv", 1) == 1:
                        del info[current_parameter]
                elif current_parameter in ["depth", "seldepth", "time",
                                           "nodes", "currmovenumber",
                                           "hashfull", "nps", "tbhits",
                                           "cpuload"]:
                    # Integer parameters
                    info[current_parameter] = int(token)
                elif current_parameter == "multipv":
                    # Ignore. Handled before.
                    pass
                elif current_parameter == "score":
                    # Score
                    if not "score" in info:
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
            logging.warn("Unknown command: %s %s", command, arg)

def set_variant_options(p, job):
    variant = job["variant"].lower()
    setoption(p, "UCI_Chess960", variant == "chess960")
    setoption(p, "UCI_Atomic", variant == "atomic")
    setoption(p, "UCI_Horde", variant == "horde")
    setoption(p, "UCI_House", variant == "crazyhouse")
    setoption(p, "UCI_KingOfTheHill", variant == "kingofthehill")
    setoption(p, "UCI_Race", variant == "racingkings")
    setoption(p, "UCI_3Check", variant == "threecheck")

def analyse(p, conf, job):
    set_variant_options(p, job)
    setoption(p, "Skill Level", 20)
    isready(p)

    send(p, "ucinewgame")
    isready(p)

    moves = job["moves"].split(" ")
    result = []

    for ply in range(len(moves), -1, -1):
        logging.info("Analysing http://lichess.org/%s#%d" % (job["game_id"], ply))
        part = go(p, conf, job["position"], moves[0:ply], True)
        result.insert(0, part)

    return result

def bestmove(p, conf, job):
    set_variant_options(p, job)
    setoption(p, "Skill Level", int(round((job["work"]["level"] - 1) * 20.0 / 7)))
    isready(p)

    send(p, "ucinewgame")
    isready(p)

    moves = job["moves"].split(" ")

    logging.info("Playing http://lichess.org/%s level %s" % (job["game_id"], job["work"]["level"]))
    part = go(p, conf, job["position"], moves, False)
    info = {}
    info["bestmove"] = part["bestmove"]
    return info


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

def make_request(conf, engine_info):
    return {
        "fishnet": {
            "version": __version__,
            "apikey": conf.get("Fishnet", "Apikey"),
        },
        "engine": engine_info
    }

def handle_response(p, response, conf, engine_info):
    if response.status == 404:
        raise NoJobFound()
    assert response.status == 200, "HTTP %d" % response.status
    data = response.read().decode("utf-8")
    logging.debug("Got job: %s" % data)
    job = json.loads(data)
    request = make_request(conf, engine_info)
    if job["work"]["type"] == "analysis":
        request["analysis"] = analyse(p, conf, job)
        quit(p)
        url = urlparse.urljoin(conf.get("Fishnet", "Endpoint"), "analysis") + "/" + str(job["work"]["id"])
        with http_request("POST", url, json.dumps(request)) as response:
            handle_response(p, response, conf, engine_info)
    elif job["work"]["type"] == "move":
        request["move"] = bestmove(p, conf, job)
        quit(p)
        url = urlparse.urljoin(conf.get("Fishnet", "Endpoint"), "move") + "/" + str(job["work"]["id"])
        with http_request("POST", url, json.dumps(request)) as response:
            handle_response(p, response, conf, engine_info)
    else:
        logging.error("Received invalid job %s" % job)


def work(conf):
    p = open_process(conf)
    engine_info = uci(p)
    logging.info("Started engine process %d: %s" % (p.pid, json.dumps(engine_info)))
    setoptions(p, conf)

    request = make_request(conf, engine_info)

    with http_request("POST", urlparse.urljoin(conf.get("Fishnet", "Endpoint"), "acquire"), json.dumps(request)) as response:
        handle_response(p, response, conf, engine_info)

def work_loop(conf):
    if not conf.has_option("Fishnet", "Movetime"):
        # Initial benchmark
        nps = bench(conf)
        logging.info("Benchmark determined nodes/second: %d", nps)
        movetime = int(3000000 * 1000 / nps)
        conf.set("Fishnet", "Movetime", str(movetime))
        logging.info("Setting movetime: %d", movetime)
    else:
        logging.info("Using movetime: %d", conf.getint("Fishnet", "Movetime"))

    backoff = 1 + random.random()

    # Continuously request and run jobs
    while True:
        try:
            work(conf)
            backoff = 1 + random.random()
        except NoJobFound:
            if conf.has_option("Fishnet", "Fixed Backoff"):
                t = conf.getfloat("Fishnet", "Fixed Backoff")
            else:
                t = 0.5 * backoff + 0.5 * backoff * random.random()
            logging.info("No job found. Backing off %0.1fs", t)
            time.sleep(t)
            backoff = min(600, backoff * 2)
        except Exception as e:
            logging.debug(e)
            if conf.has_option("Fishnet", "Fixed Backoff"):
                t = conf.getfloat("Fishnet", "Fixed Backoff")
            else:
                t = 0.8 * backoff + 0.2 * backoff * random.random()
            logging.exception("Backing off %0.1fs after exception in work loop", t)
            time.sleep(t)
            backoff = min(600, backoff * 2)


def intro():
    print("""\
  _____ _     _     _   _      _
 |  ___(_)___| |__ | \ | | ___| |_
 | |_  | / __| '_ \|  \| |/ _ \ __|
 |  _| | \__ \ | | | |\  |  __/ |_
 |_|   |_|___/_| |_|_| \_|\___|\__| %s
 Distributed Stockfish analysis for lichess.org
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

    # Get number of threads per engine process
    if conf.has_option("Engine", "Threads"):
        threads_per_process = max(conf.getint("Engine", "Threads"), 1)
    else:
        threads_per_process = 1

    # Determine the number of spare cores
    num_cores = multiprocessing.cpu_count() - 1
    max_num_processes = num_cores // threads_per_process
    if max_num_processes == 0:
        logging.warn("Not enough cores to exclusively run %d engine threads",
                     threads_per_process)
        max_num_processes = 1

    # Determine the number of processes
    if conf.has_option("Fishnet", "Processes"):
        num_processes = conf.getint("Fishnet", "Processes")
        if num_processes > max_num_processes:
            logging.warn("Number of engine processes capped at %d",
                         max_num_processes)
            num_processes = max_num_processes
    else:
        num_processes = max_num_processes

    logging.info("Using %d engine processes with %d threads each on %d cores",
                 num_processes, threads_per_process,
                 multiprocessing.cpu_count())

    work_loop(conf)
    sys.exit(0)

    # Start engine processes
    threads = []
    for _ in range(num_processes):
        thread = threading.Thread(target=work_loop, args=[conf])
        thread.daemon = True
        thread.start()
        threads.append(thread)

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    intro()

    # Parse command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conf", type=argparse.FileType("r"), nargs="+")
    parser.add_argument("--verbose", "-v", action="store_true")

    # Run
    sys.exit(main(parser.parse_args()))
