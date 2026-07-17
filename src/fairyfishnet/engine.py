# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Fairy-Stockfish process and UCI protocol helpers."""

import logging
import os
import queue
import signal
import subprocess
import threading
import time

from .constants import (
    ENGINE_GO_FALLBACK_TIMEOUT,
    ENGINE_GO_GRACE_TIMEOUT,
    ENGINE_READY_TIMEOUT,
    ENGINE_UCI_TIMEOUT,
    NNUE_ALIAS,
    NNUE_NET,
)
from .errors import EngineTimeout
from .logging_utils import ENGINE


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

    # Prevent signal propagation from parent process.
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", None)
    if creationflags is not None:
        kwargs["creationflags"] = creationflags
    else:
        kwargs["preexec_fn"] = os.setpgrp

    with _popen_lock:  # Work around Python 2 Popen race condition
        return subprocess.Popen(command, **kwargs)


def kill_process(p):
    ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
    if ctrl_break_event is not None:
        p.send_signal(ctrl_break_event)
    else:
        os.killpg(p.pid, signal.SIGKILL)

    p.communicate()


def send(p, line):
    logging.log(ENGINE, "%s << %s", p.pid, line)
    p.stdin.write(line + "\n")
    p.stdin.flush()


def _readline_with_timeout(p, timeout):
    if timeout is None:
        return p.stdout.readline()

    result = queue.Queue(maxsize=1)

    def read_line():
        try:
            result.put((p.stdout.readline(), None))
        except Exception as err:
            result.put((None, err))

    reader = threading.Thread(target=read_line)
    reader.daemon = True
    reader.start()

    try:
        line, error = result.get(True, max(0.0, timeout))
    except queue.Empty:
        raise EngineTimeout("Timed out waiting for engine output after %0.1fs" % timeout)

    if error is not None:
        raise error
    return line


def _time_left(deadline):
    if deadline is None:
        return None
    return deadline - time.time()


def recv(p, timeout=None):
    while True:
        if timeout is not None and timeout <= 0:
            raise EngineTimeout("Timed out waiting for engine output")

        line = _readline_with_timeout(p, timeout)
        if line == "":
            raise EOFError()

        line = line.rstrip()

        logging.log(ENGINE, "%s >> %s", p.pid, line)

        if line:
            return line


def recv_uci(p, timeout=None):
    command_and_args = recv(p, timeout=timeout).split(None, 1)
    command = command_and_args[0]
    arg = command_and_args[1] if len(command_and_args) == 2 else ""
    return command, arg


def uci(p, timeout=ENGINE_UCI_TIMEOUT):
    send(p, "uci")

    engine_info = {}
    variants = set()
    deadline = time.time() + timeout if timeout is not None else None

    while True:
        command, arg = recv_uci(p, timeout=_time_left(deadline))

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


def isready(p, timeout=ENGINE_READY_TIMEOUT):
    send(p, "isready")
    deadline = time.time() + timeout if timeout is not None else None
    while True:
        command, arg = recv_uci(p, timeout=_time_left(deadline))
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


def go(
    p, position, moves, movetime=None, clock=None, depth=None, nodes=None, variant=None, chess960=False, timeout=None
):
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

    if timeout is None:
        if movetime is not None:
            timeout = movetime / 1000.0 + ENGINE_GO_GRACE_TIMEOUT
        else:
            timeout = ENGINE_GO_FALLBACK_TIMEOUT
    deadline = time.time() + timeout if timeout is not None else None

    info = {}
    info["bestmove"] = None

    while True:
        command, arg = recv_uci(p, timeout=_time_left(deadline))

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
                elif token in [
                    "depth",
                    "seldepth",
                    "time",
                    "nodes",
                    "multipv",
                    "currmove",
                    "currmovenumber",
                    "hashfull",
                    "nps",
                    "tbhits",
                    "cpuload",
                    "refutation",
                    "currline",
                    "string",
                ]:
                    current_parameter = token
                    info.pop(current_parameter, None)
                elif current_parameter in [
                    "depth",
                    "seldepth",
                    "time",
                    "nodes",
                    "currmovenumber",
                    "hashfull",
                    "nps",
                    "tbhits",
                    "cpuload",
                    "multipv",
                ]:
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
            if (
                score_kind
                and score_value is not None
                and (
                    not (lowerbound or upperbound)
                    or "score" not in info
                    or info["score"].get("lowerbound")
                    or info["score"].get("upperbound")
                )
            ):
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
