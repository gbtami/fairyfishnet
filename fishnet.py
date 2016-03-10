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

try:
    from httplib import HTTPConnection
except ImportError:
    from http.client import HTTPConnection

try:
    import configparser
except ImportError:
    import ConfigParser as configparser


INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def open_process(command):
    return subprocess.Popen(conf.get("Fishnet", "EngineCommand"),
        cwd=conf.get("Fishnet", "EngineDir"),
        stdout=subprocess.PIPE,
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
            return command_and_args[0], None
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


def isready(p):
    send(p, "isready")
    while True:
        command, _ = recv(p)
        if command == "readyok":
            break


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
        if command == "info":
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


def main(conf):
    con = HTTPConnection("127.0.0.1", 9000)
    con.request("POST", "/acquire")
    response = con.getresponse()
    assert response.status == 200, "HTTP %d" % response.status
    data = response.read().decode("utf-8")
    logging.debug("Got job: %s" % data)
    job = json.loads(data)
    con.close()

    p = open_process(conf)
    engine_info = uci(p)
    logging.info("Started engine process %d: %s" % (p.pid, json.dumps(engine_info)))
    setoptions(p, conf)

    result = {
        "analysis": analyse(p, conf, job),
        "fishnet": __version__,
        "engine": engine_info,
    }

    quit(p)

    logging.debug("Sending result: %s" % json.dumps(result, indent=2))
    con = HTTPConnection("127.0.0.1", 9000)
    con.request("POST", "/{0}".format(job["game_id"]), json.dumps(result))
    response = con.getresponse()
    assert 200 <= response.status < 300, "HTTP %d" % response.status
    con.close()


def wait(t):
    logging.info("Waiting %0.2fs" % t)
    time.sleep(t)


def main_loop(conf):
    backoff = 1 + random.random()

    while True:
        try:
            main(conf)
            backoff = 1 + random.random()
        except KeyboardInterrupt:
            return
        except:
            t = 0.8 * backoff + 0.2 * backoff * random.random()
            logging.exception("Backing off %0.1fs after exception in main loop", t)
            time.sleep(t)
            backoff = min(600, backoff * 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conf", type=argparse.FileType("r"), nargs="+")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    conf = configparser.SafeConfigParser()
    for c in args.conf:
        conf.readfp(c, c.name)

    main_loop(conf)
