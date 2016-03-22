#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=wrong-import-order

"""Distributed analysis for lichess.org"""


from __future__ import print_function
from __future__ import division


__version__ = "1.2.1"

__author__ = "Niklas Fiekas"
__email__ = "niklas.fiekas@backscattering.de"
__license__ = "MIT"


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
import stat
import math
import platform

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
    import urllib.request as urllib
except ImportError:
    import urllib

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

try:
    input = raw_input
except NameError:
    pass


class LogFormatter(logging.Formatter):
    def format(self, record):
        msg = super(LogFormatter, self).format(record)

        if record.threadName == "MainThread" and record.levelno == logging.INFO:
            return "[fishnet %s] %s" % (__version__, msg)
        else:
            return "%s: %s: %s" % (record.threadName, record.levelname, msg)


def base_url(url):
    url_info = urlparse.urlparse(url)
    return "%s://%s/" % (url_info.scheme, url_info.hostname)


class HttpError(Exception):
    def __init__(self, status, reason, body):
        self.status = status
        self.reason = reason
        self.body = body

    def __str__(self):
        return "HTTP %d %s\n\n%s" % (self.status, self.reason, self.body)

    def __repr__(self):
        return "%s(%d, %r, %r)" % (type(self).__name__, self.status, self.reason, self.body)


class HttpServerError(HttpError):
    pass


class HttpClientError(HttpError):
    pass


class ConfigError(Exception):
    pass


@contextlib.contextmanager
def http(method, url, body=None, headers=None):
    logging.debug("HTTP request: %s %s, body: %s", method, url, body)

    url_info = urlparse.urlparse(url)
    if url_info.scheme == "https":
        con = httplib.HTTPSConnection(url_info.hostname, url_info.port or 443)
    else:
        con = httplib.HTTPConnection(url_info.hostname, url_info.port or 80)

    headers_with_useragent = {"User-Agent": "fishnet %s" % __version__}
    if headers:
        headers_with_useragent.update(headers)

    con.request(method, url_info.path, body, headers_with_useragent)
    response = con.getresponse()
    logging.debug("HTTP response: %d %s", response.status, response.reason)

    try:
        if 400 <= response.status < 500:
            raise HttpClientError(response.status, response.reason, response.read())
        elif 500 <= response.status < 600:
            raise HttpServerError(response.status, response.reason, response.read())
        else:
            yield response
    finally:
        con.close()


def start_backoff(conf):
    if conf.has_option("Fishnet", "Fixed Backoff"):
        while True:
            yield random.random() * conf.getfloat("Fishnet", "Fixed Backoff")
    else:
        backoff = 1
        while True:
            yield 0.5 * backoff + 0.5 * backoff * random.random()
            backoff = min(backoff + 1, 60)


def endpoint(conf, sub=""):
    endpoint = conf.get("Fishnet", "Endpoint")
    if not endpoint.endswith("/"):
        endpoint += "/"

    return urlparse.urljoin(endpoint, sub)


def popen_engine(conf, _popen_lock=threading.Lock()):
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
            logging.warning("Unexpected engine output: %s %s", command, arg)


def isready(p):
    send(p, "isready")
    while True:
        command, arg = recv(p)
        if command == "readyok":
            break
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def setoption(p, name, value):
    if value is True:
        value = "true"
    elif value is False:
        value = "false"
    elif value is None:
        value = "none"

    send(p, "setoption name %s value %s" % (name, value))


def depth(level):
    if level in [1, 2]:
        return 1
    elif level == 3:
        return 2
    elif level == 4:
        return 3
    elif level == 5:
        return 5
    elif level == 6:
        return 8
    elif level == 7:
        return 13
    elif level == 8:
        return 21
    else:  # Analysis
        return 99


def go(p, position, moves, movetime=None, depth=None, nodes=None):
    send(p, "position fen %s moves %s" % (position, " ".join(moves)))
    isready(p)

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
    send(p, " ".join(builder))

    info = {}
    info["bestmove"] = None

    while True:
        command, arg = recv(p)

        if command == "bestmove":
            bestmove = arg.split()[0]
            if bestmove and bestmove != "(none)":
                info["bestmove"] = bestmove

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

            # Stop immediately in mated positions
            if info["score"].get("mate") == 0 and info.get("multipv", 1) == 1:
                send(p, "stop")
                while True:
                    command, arg = recv(p)
                    if command == "info":
                        logging.info("Ignoring superfluous info: %s", arg)
                    elif command == "bestmove":
                        if not "(none)" in arg:
                            logging.info("Ignoring bestmove: %s", arg)

                        isready(p)
                        return info
                    else:
                        logging.warning("Unexpected engine output: %s %s", command, arg)
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def set_variant_options(p, job):
    variant = job.get("variant", "standard").lower()
    setoption(p, "UCI_Chess960", variant == "chess960")
    setoption(p, "UCI_Atomic", variant == "atomic")
    setoption(p, "UCI_Horde", variant == "horde")
    setoption(p, "UCI_House", variant == "crazyhouse")
    setoption(p, "UCI_KingOfTheHill", variant == "kingofthehill")
    setoption(p, "UCI_Race", variant == "racingkings")
    setoption(p, "UCI_3Check", variant == "threecheck")


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

    def set_engine_options(self):
        for name, value in self.engine_info["options"].items():
            setoption(self.process, name, value)

    def run(self):
        while True:
            try:
                # Check if engine is still alive
                if self.process:
                    self.process.poll()

                # Restart the engine
                if not self.process or self.process.returncode is not None:
                    self.start_engine()

                # Do the next work unit
                path, request = self.work()

                # Report result and fetch next job
                with http("POST", endpoint(self.conf, path), json.dumps(request)) as response:
                    if response.status == 204:
                        self.job = None
                        t = next(self.backoff)
                        logging.debug("No job found. Backing off %0.1fs", t)
                        time.sleep(t)
                    else:
                        data = response.read().decode("utf-8")
                        logging.debug("Got job: %s", data)

                        self.job = json.loads(data)
                        self.backoff = start_backoff(self.conf)
            except HttpServerError as err:
                self.job = None
                t = next(self.backoff)
                logging.error("Server error: HTTP %d %s. Backing off %0.1fs", err.status, err.reason, t)
                time.sleep(t)
            except HttpClientError as err:
                self.job = None
                t = next(self.backoff)
                try:
                    logging.error(json.loads(err.body.decode("utf-8"))["error"])
                except:
                    logging.error("Client error: HTTP %d %s. Backing off %0.1fs. Request was: %s", err.status, err.reason, t, json.dumps(request))
                time.sleep(t)
            except:
                self.job = None
                t = next(self.backoff)
                logging.exception("Backing off %0.1fs after exception in worker", t)
                time.sleep(t)

                # If in doubt, restart engine
                self.process.kill()

    def start_engine(self):
        self.process = popen_engine(self.conf)
        self.engine_info = uci(self.process)
        logging.info("Started engine process, pid: %d, threads: %d, identification: %s",
                     self.process.pid, self.threads, self.engine_info.get("name", "<none>"))

        # Prepare UCI options
        self.engine_info["options"] = {}
        for name, value in self.conf.items("Engine"):
            self.engine_info["options"][name] = value

        self.engine_info["options"]["threads"] = str(self.threads)

        # Set UCI options
        self.set_engine_options()

        isready(self.process)

    def make_request(self):
        return {
            "fishnet": {
                "version": __version__,
                "apikey": self.conf.get("Fishnet", "Apikey"),
            },
            "engine": self.engine_info
        }

    def work(self):
        result = self.make_request()

        if self.job and self.job["work"]["type"] == "analysis":
            result["analysis"] = self.analysis(self.job)
            return "analysis" + "/" + self.job["work"]["id"], result
        elif self.job and self.job["work"]["type"] == "move":
            result["move"] = self.bestmove(self.job)
            return "move" + "/" + self.job["work"]["id"], result
        else:
            if self.job:
                logging.error("Invalid job type: %s", job)

            return "acquire", result

    def bestmove(self, job):
        lvl = job["work"]["level"]
        set_variant_options(self.process, job)
        setoption(self.process, "Skill Level", int(round((lvl - 1) * 20.0 / 7)))
        isready(self.process)

        moves = job["moves"].split(" ")

        movetime = int(round(4000.0 / (self.threads * 0.9 ** (self.threads - 1)) / 10.0 * lvl / 8.0))

        logging.info("Playing %s%s with level %d and movetime %d ms",
                     base_url(endpoint(self.conf)), job["game_id"],
                     lvl, movetime)

        part = go(self.process, job["position"], moves,
                  movetime=movetime, depth=depth(lvl))

        self.nodes += part.get("nodes", 0)
        self.positions += 1

        return {
            "bestmove": part["bestmove"],
        }

    def analysis(self, job):
        set_variant_options(self.process, job)
        setoption(self.process, "Skill Level", 20)
        isready(self.process)

        send(self.process, "ucinewgame")
        isready(self.process)

        moves = job["moves"].split(" ")
        result = []

        start = time.time()

        for ply in range(len(moves), -1, -1):
            logging.info("Analysing %s%s#%d",
                         endpoint(self.conf), job["game_id"], ply)

            part = go(self.process, job["position"], moves[0:ply],
                      nodes=3000000, movetime=4000)

            if "mate" not in part["score"] and "time" in part and part["time"] < 100:
                logging.warning("Very low time reported: %d ms.", part["time"])

            if "nps" in part and part["nps"] >= 100000000:
                logging.warning("Dropping exorbitant nps: %d", part["nps"])
                del part["nps"]

            self.nodes += part.get("nodes", 0)
            self.positions += 1

            result.insert(0, part)

        end = time.time()
        logging.info("Time taken for %s%s: %0.1fs (%0.1fs per position)",
                     base_url(endpoint(self.conf)), job["game_id"],
                     end - start, (end - start) / (len(moves) + 1))

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


def default_config():
    conf = configparser.SafeConfigParser()

    conf.add_section("Fishnet")
    conf.set("Fishnet", "EngineDir", os.path.dirname(os.path.abspath(".")))
    conf.set("Fishnet", "Endpoint", "http://en.lichess.org/fishnet/")
    conf.set("Fishnet", "Cores", "auto")

    conf.add_section("Engine")

    return conf


def stockfish_filename():
    if os.name == "posix":
        base = "stockfish-%s" % platform.machine()
        with open("/proc/cpuinfo") as cpu_info:
            for line in cpu_info:
                if line.startswith("flags") and "bmi2" in line and "popcnt" in line:
                    return base + "-bmi2"
                if line.startswith("flags") and "popcnt" in line:
                    return base + "-modern"
        return base
    elif os.name == "nt":
        return "stockfish-%s.exe" % platform.machine()


def update_stockfish(conf, filename):
    path = os.path.join(conf.get("Fishnet", "EngineDir"), filename)

    headers = {}

    # Only update to newer versions
    try:
        headers["If-Modified-Since"] = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(os.path.getmtime(path)))
    except OSError:
        pass

    # Escape GitHub API rate limiting
    if "GITHUB_API_TOKEN" in os.environ:
        headers["Authorization"] = "token %s" % os.environ["GITHUB_API_TOKEN"]

    # Find latest release
    filename = stockfish_filename()
    logging.info("Looking up %s ...", filename)

    with http("GET", "https://api.github.com/repos/niklasf/Stockfish/releases/latest", headers=headers) as response:
        if response.status == 304:
            logging.info("Local %s is newer than release", filename)
            return filename

        release = json.loads(response.read().decode("utf-8"))

    logging.info("Latest stockfish release is tagged %s", release["tag_name"])

    for asset in release["assets"]:
        if asset["name"] == filename:
            logging.info("Found %s" % asset["browser_download_url"])
            break
    else:
        logging.error("Did not find a precompiled Stockfish for your architecture: %s", filename)
        logging.error("You might want to build an instance yourself and run with --stockfish")
        raise ConfigError("EngineCommand required (no precompiled %s)" % filename)

    # Download
    logging.info("Downloading %s ...", filename)
    def reporthook(a, b, c):
        sys.stdout.write("\rDownloading %s: %d/%d (%d%%)" % (filename, a * b, c, round(min(a * b, c) * 100 / c)))
        sys.stdout.flush()

    urllib.urlretrieve(asset["browser_download_url"], path, reporthook)

    sys.stdout.write("\n")
    sys.stdout.flush()

    # Make executable
    logging.info("chmod +x %s", filename)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC)
    return filename


def ensure_stockfish(conf):
    if not os.path.isdir(conf.get("Fishnet", "EngineDir")):
        raise ConfigError("EngineDir not found: %s", conf.get("Fishnet", "EngineDir"))

    # No fixed path configured. Download latest version
    if not conf.has_option("Fishnet", "EngineCommand"):
        conf.set("Fishnet", "EngineCommand", os.path.join(".", update_stockfish(conf, stockfish_filename())))

    # Ensure the required options are supported
    process = popen_engine(conf)
    options = []
    send(process, "uci")
    while True:
        command, arg = recv(process)

        if command == "uciok":
            break
        elif command in ["id", "Stockfish"]:
            pass
        elif command == "option":
            name = []
            for token in arg.split(" ")[1:]:
                if name and token == "type":
                    break
                name.append(token)
            options.append(" ".join(name))
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)
    process.kill()

    logging.debug("Supported options: %s", ", ".join(options))

    required_options = ["UCI_Chess960", "UCI_Atomic", "UCI_Horde", "UCI_House",
                        "UCI_KingOfTheHill", "UCI_Race", "UCI_3Check",
                        "Threads", "Hash"]

    for required_option in required_options:
        if required_option not in options:
            raise ConfigError("Unsupported engine option %s. Ensure you are using lichess custom Stockfish", required_option)


def ensure_apikey(conf):
    if not conf.has_option("Fishnet", "Apikey"):
        while True:
            apikey = input("Enter your API key (to be saved in ~/.fishnet.ini): ")
            apikey = apikey.strip()

            try:
                # TODO:
                #with http("GET", endpoint(conf, "key/%s" % apikey)) as response:
                if True:
                    conf.set("Fishnet", "Apikey", apikey)

                    persistent_conf = configparser.SafeConfigParser()
                    persistent_conf.add_section("Fishnet")
                    persistent_conf.set("Fishnet", "Apikey", apikey)
                    with open(os.path.expanduser("~/.fishnet.ini"), "w") as out:
                        persistent_conf.write(out)

                    break
            except HttpClientError as error:
                if error.status == 404:
                    print("Invalid Apikey. Try again ...")
                else:
                    logging.exception("Could not verify Apikey")

    return conf.get("Fishnet", "Apikey")


def main(args):
    # Setup logging
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LogFormatter())
    logger.addHandler(handler)

    # Parse configuration
    conf = default_config()
    conf.read(os.path.expanduser("~/.fishnet.ini"))
    conf.read("/etc/fishnet.ini")
    conf.read(os.path.expanduser("~/fishnet.ini"))
    if args.polyglot:
        conf.readfp(args.polyglot, args.polyglot.name)
    if args.cores:
        conf.set("Fishnet", "Cores", args.cores)
    if args.memory:
        conf.set("Fishnet", "Memory", args.memory)
    if args.endpoint:
        conf.set("Fishnet", "Endpoint", args.endpoint)
    if args.apikey:
        conf.set("Fishnet", "Apikey", args.apikey)
    if args.engine_dir:
        conf.set("Fishnet", "EngineDir", args.engine_dir)
    if args.engine_command:
        conf.set("Fishnet", "EngineCommand", args.engine_command)

    # Ensure Stockfish is available
    ensure_stockfish(conf)

    # Ensure Apikey is set
    ensure_apikey(conf)

    # Log custom UCI options
    for name, value in conf.items("Engine"):
        logging.warning("Using custom UCI option: name %s value %s", name, value)

    # Determine number of cores to use for engine threads
    if conf.get("Fishnet", "Cores").lower() == "auto":
        spare_threads = multiprocessing.cpu_count() - 1
    elif conf.get("Fishnet", "Cores").lower() == "all":
        spare_threads = multiprocessing.cpu_count()
    else:
        spare_threads = conf.getint("Fishnet", "Cores")

    if spare_threads == 0:
        logging.warning("Not enough cores to exclusively run an engine thread")
        spare_threads = 1
    elif spare_threads > multiprocessing.cpu_count():
        logging.warning("Using more threads than cores: %d/%d", spare_threads, multiprocessing.cpu_count())
    else:
        logging.info("Using %d cores", spare_threads)

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
        logging.warning("Very small hashtable size per engine process: %d MB", memory_per_process)
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
        logging.info("Good bye. Aborting pending jobs ...")
        for worker in workers:
            job = worker.job
            if job:
                with http("POST", endpoint(conf, "abort/%s" % job["work"]["id"]), json.dumps(worker.make_request())) as response:
                    logging.info(" - Aborted %s" % job["work"]["id"])
        return 0


if __name__ == "__main__":
    intro()

    # Parse command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(dest="polyglot", metavar="polyglot.ini",
                        type=argparse.FileType("r"), nargs="?",
                        help="polylgot.ini engine configuration file")
    parser.add_argument("--cores", help="number of cores to use for engine processes (or auto for n - 1, or all for n)")
    parser.add_argument("--memory", help="total number of memory (MB) to use for engine hashtables")
    parser.add_argument("--endpoint", help="lichess http endpoint")
    parser.add_argument("--apikey", help="fishnet api key")
    parser.add_argument("--engine-dir", help="engine working directory")
    parser.add_argument("--engine-command", help="engine command (default: download precompiled Stockfish)")
    parser.add_argument("--verbose", "-v", action="store_true", help="enable verbose log output")

    # Run
    sys.exit(main(parser.parse_args()))
