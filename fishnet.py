#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Crowd sources analysis for lichess.org"""

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


class WorkUnit(object):
    def __init__(self, variant, game_id, starting_fen, uci_moves):
        self.variant = variant
        self.game_id = game_id
        self.starting_fen = starting_fen
        self.uci_moves = uci_moves


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

    engine = {}
    while True:
        command, arg = recv(p)

        if command == "uciok":
            return engine
        elif command == "id":
            name_and_value = arg.split(None, 1)
            if len(name_and_value) == 2:
                engine[name_and_value[0]] = name_and_value[1]


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


def analyse(p, conf, unit):
    # TODO: Setup for variant

    send(p, "ucinewgame")
    send(p, "isready")

    result = []

    for ply in range(len(unit.uci_moves), -1, -1):
        logging.info("Analysing http://lichess.org/%s#%d" % (unit.game_id, ply))
        part = go(p, conf, unit.starting_fen, unit.uci_moves[0:ply])
        result.insert(0, part)

    return result

    print(result)


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
    p = open_process(conf)
    logging.info("Started engine process %d: %s" % (p.pid, json.dumps(uci(p))))
    setoptions(p, conf)

    con = HTTPConnection("127.0.0.1", 9000)
    con.request("GET", "/")
    response = con.getresponse()
    assert response.status == 200
    d = json.loads(response.read().decode("utf-8"))

    unit = WorkUnit(d["variant"], d["game_id"], d["position"], d["moves"])
    result = analyse(p, conf, unit)

    quit(p)


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
            logging.exception("Backing off %0.1f after exception in main loop", t)
            time.sleep(t)
            backoff = min(600, backoff * 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conf", type=argparse.FileType("r"), nargs="+")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)

    conf = configparser.SafeConfigParser()
    for c in args.conf:
        conf.readfp(c, c.name)

    main_loop(conf)
