#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Distributed analysis for lichess.org"""

__version__ = "0.9.0"

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


class NoJobFound(Exception):
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
    yield con.getresponse()
    con.close()


def available_ram():
    try:
        with open("/proc/meminfo") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    label, ram, unit = line.split()
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
    if not level: # analysis
        return time
    # For play, divide analysis time per 10, then scale to level
    return int(round(time / 10.0 * level / 8.0))


def depth(level):
    if not level: # analysis
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


def go(p, conf, starting_fen, uci_moves, is_analysis, level):
    send(p, "position fen %s moves %s" % (starting_fen, " ".join(uci_moves)))
    isready(p)
    send(p, "go movetime %d depth %d" % (movetime(conf, level), depth(level)))

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
        elif command == "info" and not is_analysis:
            continue
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


def analyse(p, conf, job):
    set_variant_options(p, job)
    setoption(p, "Skill Level", 20)
    isready(p)

    send(p, "ucinewgame")
    isready(p)

    moves = job["moves"].split(" ")
    result = []

    for ply in range(len(moves), -1, -1):
        logging.info("Analysing %s%s#%d",
                     base_url(conf.get("Fishnet", "Endpoint")),
                     job["game_id"], ply)

        part = go(p, conf, job["position"], moves[0:ply], True, None)
        result.insert(0, part)

    return result


def bestmove(p, conf, job):
    set_variant_options(p, job)
    setoption(p, "Skill Level", int(round((job["work"]["level"] - 1) * 20.0 / 7)))
    isready(p)

    send(p, "ucinewgame")
    isready(p)

    moves = job["moves"].split(" ")

    logging.info("Playing %s%s level %s",
                 base_url(conf.get("Fishnet", "Endpoint")),
                 job["game_id"], job["work"]["level"])

    part = go(p, conf, job["position"], moves, False, job["work"]["level"])
    return {
        "bestmove": part["bestmove"],
    }


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


def work(p, conf, engine_info, job):
    result = {
        "fishnet": {
            "version": __version__,
            "apikey": conf.get("Fishnet", "Apikey"),
        },
        "engine": engine_info
    }

    if job and job["work"]["type"] == "analysis":
        result["analysis"] = analyse(p, conf, job)
        return "analysis" + "/" + job["work"]["id"], result
    elif job and job["work"]["type"] == "move":
        result["move"] = bestmove(p, conf, job)
        return "move" + "/" + job["work"]["id"], result
    else:
        if job:
            logging.error("Invalid job type: %s", job)

        return "acquire", result


def start_engine(conf, threads):
    p = open_process(conf)
    engine_info = uci(p)
    logging.info("Started engine process, pid: %d, threads: %d, identification: %s",
                 p.pid, threads, engine_info.get("name", "<none>"))

    # Prepare UCI options
    engine_info["options"] = {}
    for name, value in conf.items("Engine"):
        engine_info["options"][name] = value

    engine_info["options"]["threads"] = str(threads)

    # Set UCI options
    for name, value in engine_info["options"].items():
        setoption(p, name, value)

    isready(p)

    return p, engine_info


def work_loop(conf, threads):
    p, engine_info = start_engine(conf, threads)

    # Determine movetime by benchmark or config
    if not conf.has_option("Fishnet", "Movetime"):
        logging.info("Running benchmark ...")
        nps = bench(p)
        logging.info("Benchmark determined nodes/second: %d", nps)
        movetime = int(6000000 * 1000 // (nps * threads * 0.9 ** (threads - 1)))
        conf.set("Fishnet", "Movetime", str(movetime))
        logging.info("Setting movetime: %d", movetime)
    else:
        logging.info("Using movetime: %d", conf.getint("Fishnet", "Movetime"))

    backoff = start_backoff(conf)
    job = None
    while True:
        try:
            path, request = work(p, conf, engine_info, job)

            with http_request("POST", urlparse.urljoin(conf.get("Fishnet", "Endpoint"), path), json.dumps(request)) as response:
                if response.status == 204:
                    raise NoJobFound()
                assert response.status in [200, 202], "HTTP %d" % response.status
                data = response.read().decode("utf-8")
                logging.debug("Got job: %s", data)

            job = json.loads(data)
            backoff = start_backoff(conf)
        except NoJobFound:
            job = None
            t = next(backoff)
            logging.info("No job found. Backing off %0.1fs", t)
            time.sleep(t)
        except:
            job = None
            t = next(backoff)
            logging.exception("Backing off %0.1fs after exception in work loop", t)
            time.sleep(t)

            # If in doubt, restart engine
            p.kill()
            p, engine_info = start_engine(conf, threads)


def intro():
    print("""\

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
    spare_cores = multiprocessing.cpu_count() - 1
    logging.info("Cores: %d + 1", spare_cores)
    if spare_cores == 0:
        logging.warn("No spare core to exclusively run an engine process")
        spare_cores = 1  # Run 1, anyway

    if conf.has_option("Fishnet", "Processes"):
        spare_processes = max(conf.getint("Fishnet", "Processes"), 1)
        logging.info("Number of processes capped at: %d", spare_processes)
    else:
        spare_processes = spare_cores

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
        worker = threading.Thread(target=work_loop, args=[conf, threads_per_process])
        worker.daemon = True
        workers.append(worker)

        spare_cores -= threads_per_process
        spare_processes -= 1
        spare_memory -= memory_per_process

    # Use the rest of the cores
    if spare_cores > 0 and spare_processes > 0 and spare_memory > memory_per_process:
        worker = threading.Thread(target=work_loop, args=[conf, spare_cores])
        worker.daemon = True
        workers.append(worker)

    if not workers:
        logging.error("Not enough resources to start a worker")
        return 1

    # Start all threads and wait forever
    for i, worker in enumerate(workers):
        worker.name = "Process %d" % (i + 1)
        worker.start()
    try:
        while True:
            time.sleep(60)
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
