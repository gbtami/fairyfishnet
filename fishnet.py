#!/usr/bin/env python

import argparse
import logging
import subprocess

try:
    import configparser
except ImportError:
    import ConfigParser as configparser


INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class WorkUnit(object):
    def __init__(self, variant, starting_fen, uci_moves):
        self.variant = variant
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
    line = p.stdout.readline().rstrip()
    logging.debug("%s >> %s", p.pid, line)
    return line.rstrip()


def uci(p):
    send(p, "uci")
    while True:
        line = recv(p)
        if line == "uciok":
            break


def isready(p):
    send(p, "isready")
    while True:
        line = recv(p)
        if line == "readyok":
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


def go(p, starting_fen, uci_moves):
    send(p, "position fen %s moves %s" % (starting_fen, " ".join(uci_moves)))
    send(p, "go movetime 1000")

    info = {}
    info["score"] = {}

    while True:
        line = recv(p)
        command_and_args = line.split(None, 1)
        if not command_and_args:
            return

        command = command_and_args[0]

        if command == "bestmove":
            return info
        if command == "info":
            arg = command_and_args[1] if len(command_and_args) > 1 else ""

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


def analyse(p, unit):
    # TODO: Setup for variant

    send(p, "ucinewgame")
    send(p, "isready")

    result = []

    for ply in range(len(unit.uci_moves), -1, -1):
        result.insert(0, go(p, unit.starting_fen, unit.uci_moves[0:ply]))

    print(result)


def main(conf):
    p = open_process(conf)
    uci(p)
    setoptions(p, conf)
    analyse(p, WorkUnit("standard", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", ["e2e4", "g8f6", "e4e5"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crowd sourced analysis for lichess.org")
    parser.add_argument("conf", type=argparse.FileType("r"), nargs="+")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)

    conf = configparser.SafeConfigParser()
    for c in args.conf:
        conf.readfp(c, c.name)

    main(conf)
