# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Worker threads, job execution, and progress reporting."""

import json
import logging
import platform
import queue
import threading
import time
from typing import Any, Dict, List

from .config import get_endpoint, get_engine_dir, get_key, get_stockfish_command, start_backoff
from .constants import (
    ABORT_REASON_ENGINE_CRASH,
    ABORT_REASON_ENGINE_TIMEOUT,
    ABORT_REASON_VARIANTS_UNAVAILABLE,
    HTTP_TIMEOUT,
    LVL_DEPTHS,
    LVL_MOVETIMES,
    LVL_SKILL,
    NNUE_NET,
    PROGRESS_REPORT_INTERVAL,
    __version__,
)
from .dependencies import HTTPAdapter, requests
from .engine import (
    go,
    isready,
    kill_process,
    modded_variant,
    open_process,
    send,
    set_variant_options,
    setoption,
    uci,
)
from .errors import DEAD_ENGINE_ERRORS, EngineTimeout, JsonResponseError, UpdateRequired, VariantsIniError
from .http_utils import base_url, response_json
from .logging_utils import PROGRESS
from .variants import pyffish_get_fen, use_engine_variants


class ProgressReporter(threading.Thread):
    def __init__(self, queue_size, conf):
        super(ProgressReporter, self).__init__()
        self.http = requests.Session()
        self.conf = conf

        self.queue = queue.Queue(maxsize=queue_size)
        self._poison_pill = object()

    def send(self, job, result):
        path = "analysis/%s" % job["work"]["id"]
        data = json.dumps(result).encode("utf-8")
        try:
            self.queue.put_nowait((path, data))
        except queue.Full:
            logging.debug("Could not keep up with progress reports. Dropping one.")

    def stop(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put(self._poison_pill)

    def run(self):
        while True:
            item = self.queue.get()
            if item == self._poison_pill:
                return

            path, data = item

            try:
                response = self.http.post(get_endpoint(self.conf, path), data=data, timeout=HTTP_TIMEOUT)
                if response.status_code == 429:
                    logging.error("Too many requests. Suspending progress reports for 60s ...")
                    time.sleep(60.0)
                elif response.status_code != 204:
                    logging.error("Expected status 204 for progress report, got %d", response.status_code)
            except requests.RequestException as err:
                logging.warning("Could not send progress report (%s). Continuing.", err)


class Worker(threading.Thread):
    def __init__(self, conf, threads, memory, progress_reporter):
        super(Worker, self).__init__()
        self.conf = conf
        self.threads = threads
        self.memory = memory

        self.progress_reporter = progress_reporter

        self.alive = True
        self.fatal_error = None
        self.finished = threading.Event()
        self.sleep = threading.Event()
        self.status_lock = threading.RLock()

        self.nodes = 0
        self.positions = 0

        self.stockfish_lock = threading.RLock()
        self.stockfish = None
        self.stockfish_info = None

        self.job = None
        self.backoff = start_backoff(self.conf)

        self.http = requests.Session()
        self.http.mount("http://", HTTPAdapter(max_retries=1))
        self.http.mount("https://", HTTPAdapter(max_retries=1))

    def set_name(self, name):
        self.name = name
        self.progress_reporter.name = "%s (P)" % (name,)

    def stop(self):
        with self.status_lock:
            self.alive = False
            self.kill_stockfish()
            self.sleep.set()

    def stop_soon(self):
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
            # Check if the engine is still alive and start, if necessary
            self.start_stockfish()

            # Do the next work unit
            path, request = self.work()
        except VariantsIniError as err:
            error = {
                "reason": ABORT_REASON_VARIANTS_UNAVAILABLE,
                "kind": err.__class__.__name__,
                "message": str(err),
            }
            logging.error("Could not load the exact variants.ini required by the job: %s", err)
            self.abort_job(error=error)
            self.sleep.wait(next(self.backoff))
            return
        except DEAD_ENGINE_ERRORS + (EngineTimeout,) as err:
            alive = self.is_alive()
            engine_timeout = isinstance(err, EngineTimeout)
            error: Dict[str, Any] = {
                "reason": ABORT_REASON_ENGINE_TIMEOUT if engine_timeout else ABORT_REASON_ENGINE_CRASH,
                "kind": err.__class__.__name__,
            }
            if str(err):
                error["message"] = str(err)
            if self.stockfish:
                returncode = self.stockfish.poll()
                if returncode is not None:
                    error["engine_returncode"] = returncode
            if alive:
                t = next(self.backoff)
                if engine_timeout:
                    logging.exception("Engine process timed out. Backing off %0.1fs", t)
                else:
                    logging.exception("Engine process has died. Backing off %0.1fs", t)

            # Tell server this abort is from an engine failure so it can cap retries
            # and avoid rescheduling the same crashing/hanging position forever.
            self.abort_job(error=error)

            if alive:
                if engine_timeout:
                    # Kill immediately so the blocked stdout reader can unwind.
                    self.kill_stockfish()
                    self.sleep.wait(t)
                else:
                    self.sleep.wait(t)
                    self.kill_stockfish()

            return

        try:
            # Report result and fetch next job
            response = self.http.post(get_endpoint(self.conf, path), json=request, timeout=HTTP_TIMEOUT)
        except requests.RequestException as err:
            self.job = None
            t = next(self.backoff)
            logging.error("Backing off %0.1fs after failed request (%s)", t, err)
            self.sleep.wait(t)
        else:
            if response.status_code == 204:
                self.job = None
                t = next(self.backoff)
                logging.debug("No job found. Backing off %0.1fs", t)
                self.sleep.wait(t)
            elif response.status_code == 202:
                logging.debug("Got job: %s", response.text)
                try:
                    self.job = response_json(response, "fishnet acquire")
                except JsonResponseError as err:
                    self.job = None
                    t = next(self.backoff)
                    logging.error("%s. Backing off %0.1fs", err, t)
                    self.sleep.wait(t)
                else:
                    self.backoff = start_backoff(self.conf)
            elif 500 <= response.status_code <= 599:
                self.job = None
                t = next(self.backoff)
                logging.error("Server error: HTTP %d %s. Backing off %0.1fs", response.status_code, response.reason, t)
                self.sleep.wait(t)
            elif 400 <= response.status_code <= 499:
                self.job = None
                t = next(self.backoff) + (60 if response.status_code == 429 else 0)
                try:
                    logging.debug("Client error: HTTP %d %s: %s", response.status_code, response.reason, response.text)
                    error = response_json(response, "fishnet client error response")["error"]
                    logging.error(error)

                    if "Please restart fishnet to upgrade." in error:
                        logging.error("Stopping worker for update.")
                        raise UpdateRequired()
                except (KeyError, ValueError, JsonResponseError):
                    logging.error(
                        "Client error: HTTP %d %s. Backing off %0.1fs. Request was: %s",
                        response.status_code,
                        response.reason,
                        t,
                        json.dumps(request),
                    )
                self.sleep.wait(t)
            else:
                self.job = None
                t = next(self.backoff)
                logging.error("Unexpected HTTP status for acquire: %d", response.status_code)
                self.sleep.wait(t)

    def abort_job(self, error=None):
        if self.job is None:
            return

        logging.debug("Aborting job %s", self.job["work"]["id"])
        request = self.make_request()
        if error is not None:
            request["error"] = error

        try:
            response = requests.post(
                get_endpoint(self.conf, "abort/%s" % self.job["work"]["id"]),
                data=json.dumps(request),
                timeout=HTTP_TIMEOUT,
            )
            if response.status_code == 204:
                logging.info("Aborted job %s", self.job["work"]["id"])
            else:
                logging.error("Unexpected HTTP status for abort: %d", response.status_code)
        except requests.RequestException:
            logging.exception("Could not abort job. Continuing.")

        self.job = None

    def kill_stockfish(self):
        with self.stockfish_lock:
            if self.stockfish:
                try:
                    kill_process(self.stockfish)
                except OSError:
                    logging.exception("Failed to kill engine process.")
                self.stockfish = None

    def start_stockfish(self):
        with self.stockfish_lock:
            # Check if already running.
            if self.stockfish and self.stockfish.poll() is None:
                return

            # Start process
            self.stockfish = open_process(get_stockfish_command(self.conf, False), get_engine_dir(self.conf))

        self.stockfish_info, _ = uci(self.stockfish)
        self.stockfish_info.pop("author", None)
        logging.info(
            "Started %s, threads: %s (%d), pid: %d",
            self.stockfish_info.get("name", "Stockfish <?>"),
            "+" * self.threads,
            self.threads,
            self.stockfish.pid,
        )

        # Prepare UCI options
        self.stockfish_info["options"] = {}
        self.stockfish_info["options"]["threads"] = str(self.threads)
        self.stockfish_info["options"]["hash"] = str(self.memory)

        # Custom options
        if self.conf.has_section("Stockfish"):
            for name, value in self.conf.items("Stockfish"):
                self.stockfish_info["options"][name] = value

        # Add .nnue file list
        self.stockfish_info["nnue"] = ["%s-%s.nnue" % (v, NNUE_NET[v]) for v in NNUE_NET]

        # Set UCI options
        for name, value in self.stockfish_info["options"].items():
            setoption(self.stockfish, name, value)

        isready(self.stockfish)

    def make_request(self) -> Dict[str, Any]:
        return {
            "fishnet": {
                "version": __version__,
                "python": platform.python_version(),
                "apikey": get_key(self.conf),
            },
            "stockfish": self.stockfish_info,
        }

    def work(self):
        result = self.make_request()

        if self.job and self.job["work"]["type"] == "analysis":
            result = self.analysis(self.job)
            return "analysis" + "/" + self.job["work"]["id"], result
        elif self.job and self.job["work"]["type"] == "move":
            result = self.bestmove(self.job)
            return "move" + "/" + self.job["work"]["id"], result
        else:
            if self.job:
                logging.error("Invalid job type: %s", self.job["work"]["type"])

            return "acquire", result

    def job_name(self, job, ply=None):
        builder = []
        if job.get("game_id"):
            builder.append(base_url(get_endpoint(self.conf)))
            builder.append(job["game_id"])
        else:
            builder.append(job["work"]["id"])
        if ply is not None:
            builder.append("#")
            builder.append(str(ply))
        return "".join(builder)

    def bestmove(self, job):
        variant = job.get("variant", "standard")
        with use_engine_variants(
            self.stockfish,
            self.conf,
            job.get("variantsSha256"),
            job.get("variantsScope") or variant,
        ) as variants_ini:
            return self._bestmove(job, variants_ini)

    def _bestmove(self, job, variants_ini):
        lvl = job["work"]["level"]
        variant = job.get("variant", "standard")
        chess960 = job.get("chess960", False)
        fen = job["position"]
        moves = job["moves"].split(" ")
        nnue = job.get("nnue", True)

        logging.debug("Playing %s (%s) with lvl %d", self.job_name(job), variant, lvl)

        variant = modded_variant(variant, chess960, fen)
        set_variant_options(self.stockfish, variant, chess960, nnue)
        setoption(self.stockfish, "Skill Level", LVL_SKILL[lvl])
        setoption(self.stockfish, "UCI_AnalyseMode", False)
        send(self.stockfish, "ucinewgame")
        isready(self.stockfish)

        movetime = int(round(LVL_MOVETIMES[lvl] / (self.threads * 0.9 ** (self.threads - 1))))

        start = time.time()
        part = go(
            self.stockfish,
            fen,
            moves,
            movetime=movetime,
            clock=job["work"].get("clock"),
            depth=LVL_DEPTHS[lvl],
            variant=variant,
            chess960=chess960,
        )
        end = time.time()

        logging.log(
            PROGRESS,
            "Played move in %s (%s) with lvl %d: %0.3fs elapsed, depth %d",
            self.job_name(job),
            variant,
            lvl,
            end - start,
            part.get("depth", 0),
        )

        self.nodes += part.get("nodes", 0)
        self.positions += 1

        sfen = False
        show_promoted = variant in (
            "makruk",
            "makpong",
            "cambodian",
            "bughouse",
            "supply",
            "makbug",
        )
        if len(job["moves"]) > 0:
            try:
                fen = pyffish_get_fen(variants_ini, variant, fen, moves, chess960, sfen, show_promoted)
            except Exception:
                logging.error("sf.get_fen() failed on %s with moves %s", job["position"], job["moves"])

        result = self.make_request()
        result["move"] = {"bestmove": part["bestmove"], "fen": fen}
        return result

    def analysis(self, job):
        variant = job.get("variant", "standard")
        with use_engine_variants(
            self.stockfish,
            self.conf,
            job.get("variantsSha256"),
            job.get("variantsScope") or variant,
        ):
            return self._analysis(job)

    def _analysis(self, job):
        variant = job.get("variant", "standard")
        chess960 = job.get("chess960", False)
        fen = job["position"]
        moves = job["moves"].split(" ")
        nnue = job.get("nnue", True)

        result = self.make_request()
        analysis_rows: List[Any] = [None for _ in range(len(moves) + 1)]
        result["analysis"] = analysis_rows
        start = last_progress_report = time.time()

        variant = modded_variant(variant, chess960, fen)
        set_variant_options(self.stockfish, variant, chess960, nnue)
        setoption(self.stockfish, "Skill Level", 20)
        setoption(self.stockfish, "UCI_AnalyseMode", True)
        send(self.stockfish, "ucinewgame")
        isready(self.stockfish)

        nodes = job.get("nodes") or 3500000
        skip = job.get("skipPositions", [])

        num_positions = 0

        for ply in range(len(moves), -1, -1):
            if ply in skip:
                analysis_rows[ply] = {"skipped": True}
                continue

            if last_progress_report + PROGRESS_REPORT_INTERVAL < time.time():
                if self.progress_reporter:
                    self.progress_reporter.send(job, result)
                last_progress_report = time.time()

            logging.log(PROGRESS, "Analysing %s: %s", variant, self.job_name(job, ply))

            part = go(self.stockfish, fen, moves[0:ply], nodes=nodes, movetime=4000, variant=variant, chess960=chess960)

            if "mate" not in part["score"] and "time" in part and part["time"] < 100:
                logging.warning("Very low time reported: %d ms.", part["time"])

            if "nps" in part and part["nps"] >= 100000000:
                logging.warning("Dropping exorbitant nps: %d", part["nps"])
                del part["nps"]

            self.nodes += part.get("nodes", 0)
            self.positions += 1
            num_positions += 1

            analysis_rows[ply] = part

        end = time.time()

        if num_positions:
            logging.info(
                "%s took %0.1fs (%0.2fs per position)", self.job_name(job), end - start, (end - start) / num_positions
            )
        else:
            logging.info("%s done (nothing to do)", self.job_name(job))

        return result
