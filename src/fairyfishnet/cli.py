# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command-line interface and long-running worker orchestration."""

import argparse
import collections
import getpass
import logging
import os
import platform
import random
import signal
import sys
import textwrap
import time
from importlib.metadata import version as distribution_version
from shlex import quote as shell_quote

from .config import (
    conf_get,
    configure,
    get_endpoint,
    get_engine_dir,
    get_key,
    get_stockfish_command,
    load_conf,
    parse_bool,
    validate_cores,
    validate_endpoint,
    validate_engine_dir,
    validate_key,
    validate_memory,
    validate_nnue,
    validate_stockfish_command,
    validate_threads,
)
from .constants import (
    CHECK_PYPI_CHANCE,
    DEFAULT_CONFIG,
    DEFAULT_ENDPOINT,
    DEFAULT_THREADS,
    DESCRIPTION,
    STAT_INTERVAL,
    __version__,
)
from .cpuid import cmd_cpuid
from .downloads import update_available, update_self
from .errors import ConfigError, Shutdown, ShutdownSoon, UpdateRequired
from .logging_utils import intro, setup_logging
from .variants import (
    VARIANTS_CACHE_CLEANUP_INTERVAL,
    cleanup_variants_ini_cache,
    clear_variants_ini_lease,
    refresh_variants_ini_lease,
)
from .worker import ProgressReporter, Worker


class SignalHandler(object):
    def __init__(self):
        self.ignore = False

        signal.signal(signal.SIGTERM, self.handle_term)
        signal.signal(signal.SIGINT, self.handle_int)

        try:
            signal.signal(signal.SIGUSR1, self.handle_usr1)
        except AttributeError:
            # No SIGUSR1 on Windows
            pass

    def handle_int(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise ShutdownSoon()

    def handle_term(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise Shutdown()

    def handle_usr1(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise UpdateRequired()


def cmd_run(args):
    conf = load_conf(args)

    if args.auto_update:
        print()
        print("### Updating ...")
        print()
        update_self()

    stockfish_command = validate_stockfish_command(conf_get(conf, "StockfishCommand"), conf)
    if not stockfish_command:
        print()
        print("### Updating Stockfish ...")
        print()
        stockfish_command = get_stockfish_command(conf)

    # Check .nnue files
    validate_nnue()

    print()
    print("### Checking configuration ...")
    print()
    print("Python:           %s (with requests %s)" % (platform.python_version(), distribution_version("requests")))
    print("EngineDir:        %s" % get_engine_dir(conf))
    print("StockfishCommand: %s" % stockfish_command)
    print("Key:              %s" % (("*" * len(get_key(conf))) or "(none)"))

    cores = validate_cores(conf_get(conf, "Cores"))
    print("Cores:            %d" % cores)

    threads = validate_threads(conf_get(conf, "Threads"), conf)
    instances = max(1, cores // threads)
    print("Engine processes: %d (each ~%d threads)" % (instances, threads))
    memory = validate_memory(conf_get(conf, "Memory"), conf)
    print("Memory:           %d MB" % memory)
    endpoint = get_endpoint(conf)
    warning = "" if endpoint.startswith("https://") else " (WARNING: not using https)"
    print("Endpoint:         %s%s" % (endpoint, warning))
    print("FixedBackoff:     %s" % parse_bool(conf_get(conf, "FixedBackoff")))
    print()

    if conf.has_section("Stockfish") and conf.items("Stockfish"):
        print("Using custom UCI options is discouraged:")
        for name, value in conf.items("Stockfish"):
            if name.lower() == "hash":
                hint = " (use --memory instead)"
            elif name.lower() == "threads":
                hint = " (use --threads-per-process instead)"
            else:
                hint = ""
            print(" * %s = %s%s" % (name, value, hint))
        print()

    cleanup_variants_ini_cache(conf)
    last_variants_cleanup = time.time()

    print("### Starting workers ...")
    print()

    buckets = [0] * instances
    for i in range(0, cores):
        buckets[i % instances] += 1

    progress_reporter = ProgressReporter(len(buckets) + 4, conf)
    progress_reporter.daemon = True
    progress_reporter.start()

    workers = [Worker(conf, bucket, memory // instances, progress_reporter) for bucket in buckets]

    # Start all threads
    for i, worker in enumerate(workers):
        worker.set_name("><> %d" % (i + 1))
        worker.daemon = True
        worker.start()

    # Wait while the workers are running
    handler = None
    try:
        # Let SIGTERM and SIGINT gracefully terminate the program
        handler = SignalHandler()

        try:
            while True:
                # Check worker status
                for _ in range(int(max(1, STAT_INTERVAL / len(workers)))):
                    for worker in workers:
                        worker.finished.wait(1.0)
                        if worker.fatal_error:
                            raise worker.fatal_error

                # Log stats
                logging.info(
                    "[fishnet v%s] Analyzed %d positions, crunched %d million nodes",
                    __version__,
                    sum(worker.positions for worker in workers),
                    int(sum(worker.nodes for worker in workers) / 1000 / 1000),
                )

                refresh_variants_ini_lease(conf)
                if time.time() - last_variants_cleanup >= VARIANTS_CACHE_CLEANUP_INTERVAL:
                    cleanup_variants_ini_cache(conf)
                    last_variants_cleanup = time.time()

                # Check for update
                if random.random() <= CHECK_PYPI_CHANCE and update_available() and args.auto_update:
                    raise UpdateRequired()
        except ShutdownSoon:
            handler = SignalHandler()

            if any(worker.job for worker in workers):
                logging.info("\n\n### Stopping soon. Press ^C again to abort pending jobs ...\n")

            for worker in workers:
                worker.stop_soon()

            for worker in workers:
                while not worker.finished.wait(0.5):
                    pass
    except (Shutdown, ShutdownSoon):
        if any(worker.job for worker in workers):
            logging.info("\n\n### Good bye! Aborting pending jobs ...\n")
        else:
            logging.info("\n\n### Good bye!")
    except UpdateRequired:
        if any(worker.job for worker in workers):
            logging.info("\n\n### Update required! Aborting pending jobs ...\n")
        else:
            logging.info("\n\n### Update required!")
        raise
    finally:
        if handler is not None:
            handler.ignore = True

        # Stop workers
        for worker in workers:
            worker.stop()

        progress_reporter.stop()

        # Wait
        for worker in workers:
            worker.finished.wait()

        clear_variants_ini_lease(conf)

    return 0


def cmd_configure(args):
    configure(args)
    return 0


def cmd_systemd(args):
    conf = load_conf(args)

    template = textwrap.dedent("""\
        [Unit]
        Description=Fishnet instance
        After=network-online.target
        Wants=network-online.target

        [Service]
        ExecStart={start}
        WorkingDirectory={cwd}
        ReadWriteDirectories={cwd}
        User={user}
        Group={group}
        Nice=5
        CapabilityBoundingSet=
        PrivateTmp=true
        PrivateDevices=true
        DevicePolicy=closed
        ProtectSystem={protect_system}
        NoNewPrivileges=true
        Restart=always

        [Install]
        WantedBy=multi-user.target""")

    # Prepare command line arguments
    builder = [shell_quote(sys.executable)]

    if __package__ is None:
        builder.append(shell_quote(os.path.abspath(sys.argv[0])))
    else:
        builder.extend(["-m", "fairyfishnet"])

    if args.no_conf:
        builder.append("--no-conf")
    else:
        config_file = os.path.abspath(args.conf or DEFAULT_CONFIG)
        builder.append("--conf")
        builder.append(shell_quote(config_file))

    if args.key is not None:
        builder.append("--key")
        builder.append(shell_quote(validate_key(args.key, conf)))
    if args.engine_dir is not None:
        builder.append("--engine-dir")
        builder.append(shell_quote(validate_engine_dir(args.engine_dir)))
    if args.stockfish_command is not None:
        stockfish_command = validate_stockfish_command(args.stockfish_command, conf)
        if stockfish_command is not None:
            builder.append("--stockfish-command")
            builder.append(shell_quote(stockfish_command))
    if args.cores is not None:
        builder.append("--cores")
        builder.append(shell_quote(str(validate_cores(args.cores))))
    if args.memory is not None:
        builder.append("--memory")
        builder.append(shell_quote(str(validate_memory(args.memory, conf))))
    if args.threads is not None:
        builder.append("--threads-per-process")
        builder.append(shell_quote(str(validate_threads(args.threads, conf))))
    if args.endpoint is not None:
        builder.append("--endpoint")
        builder.append(shell_quote(validate_endpoint(args.endpoint)))
    if args.fixed_backoff is not None:
        builder.append("--fixed-backoff" if args.fixed_backoff else "--no-fixed-backoff")
    for option_name, option_value in args.setoption:
        builder.append("--setoption")
        builder.append(shell_quote(option_name))
        builder.append(shell_quote(option_value))
    if args.auto_update:
        builder.append("--auto-update")

    builder.append("run")

    start = " ".join(builder)

    protect_system = "full"
    if args.auto_update and os.path.realpath(os.path.abspath(__file__)).startswith("/usr/"):
        protect_system = "false"

    print(
        template.format(
            user=getpass.getuser(),
            group=getpass.getuser(),
            cwd=os.path.abspath("."),
            start=start,
            protect_system=protect_system,
        )
    )

    try:
        if os.geteuid() == 0:
            print("\n# WARNING: Running as root is not recommended!", file=sys.stderr)
    except AttributeError:
        # No os.getuid() on Windows
        pass

    if sys.stdout.isatty():
        print("\n# Example usage:", file=sys.stderr)
        print("# python -m fairyfishnet systemd | sudo tee /etc/systemd/system/fishnet.service", file=sys.stderr)
        print("# sudo systemctl enable fishnet.service", file=sys.stderr)
        print("# sudo systemctl start fishnet.service", file=sys.stderr)
        print("#", file=sys.stderr)
        print("# Live view of the log: sudo journalctl --follow -u fishnet", file=sys.stderr)


def main(argv):
    # Parse command line arguments
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--verbose", "-v", default=0, action="count", help="increase verbosity")
    parser.add_argument("--version", action="version", version="fishnet v{0}".format(__version__))

    g = parser.add_argument_group("configuration")
    g.add_argument("--auto-update", action="store_true", help="automatically install available updates")
    g.add_argument("--conf", help="configuration file")
    g.add_argument("--no-conf", action="store_true", help="do not use a configuration file")
    g.add_argument("--key", "--apikey", "-k", help="fishnet api key")

    g = parser.add_argument_group("resources")
    g.add_argument("--cores", help="number of cores to use for engine processes (or auto for n - 1, or all for n)")
    g.add_argument("--memory", help="total memory (MB) to use for engine hashtables")

    g = parser.add_argument_group("advanced")
    g.add_argument("--endpoint", help="pychess-variants http endpoint (default: %s)" % DEFAULT_ENDPOINT)
    g.add_argument("--engine-dir", help="engine working directory")
    g.add_argument("--stockfish-command", help="stockfish command (default: download precompiled Stockfish)")
    g.add_argument(
        "--threads-per-process",
        "--threads",
        type=int,
        dest="threads",
        help="hint for the number of threads to use per engine process (default: %d)" % DEFAULT_THREADS,
    )
    g.add_argument(
        "--fixed-backoff", action="store_true", default=None, help="fixed backoff (only recommended for move servers)"
    )
    g.add_argument("--no-fixed-backoff", dest="fixed_backoff", action="store_false", default=None)
    g.add_argument(
        "--setoption",
        "-o",
        nargs=2,
        action="append",
        default=[],
        metavar=("NAME", "VALUE"),
        help="set a custom uci option",
    )

    commands = collections.OrderedDict(
        [
            ("run", cmd_run),
            ("configure", cmd_configure),
            ("systemd", cmd_systemd),
            ("cpuid", cmd_cpuid),
        ]
    )

    parser.add_argument("command", default="run", nargs="?", choices=commands.keys())

    args = parser.parse_args(argv[1:])

    # Setup logging
    setup_logging(args.verbose, sys.stderr if args.command == "systemd" else sys.stdout)

    # Show intro
    if args.command not in ["systemd", "cpuid"]:
        print(intro())
        sys.stdout.flush()

    # Run
    try:
        sys.exit(commands[args.command](args))
    except UpdateRequired:
        if args.auto_update:
            logging.info("\n\n### Updating ...\n")
            update_self()

        logging.error("Update required. Exiting (status 70)")
        return 70
    except ConfigError:
        logging.exception("Configuration error")
        return 78
    except (KeyboardInterrupt, Shutdown, ShutdownSoon):
        return 0


def entrypoint():
    return main(sys.argv)
