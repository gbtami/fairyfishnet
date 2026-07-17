# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Console and diagnostic logging helpers."""

import collections
import logging
import sys

from .constants import __version__


def intro():
    return (
        r"""
.   _________         .    .
.  (..       \_    ,  |\  /|
.   \       O  \  /|  \ \/ /
.    \______    \/ |   \  /      _____ _     _     _   _      _
.       vvvv\    \ |   /  |     |  ___(_)___| |__ | \ | | ___| |_
.       \^^^^  ==   \_/   |     | |_  | / __| '_ \|  \| |/ _ \ __|
.        `\_   ===    \.  |     |  _| | \__ \ | | | |\  |  __/ |_
.        / /\_   \ /      |     |_|   |_|___/_| |_|_| \_|\___|\__| %s
.        |/   \_  \|      /
.               \________/      Distributed Fairy-Stockfish analysis for pychess-variants
""".lstrip()
        % __version__
    )


PROGRESS = 15
ENGINE = 5
logging.addLevelName(PROGRESS, "PROGRESS")
logging.addLevelName(ENGINE, "ENGINE")


class LogFormatter(logging.Formatter):
    def format(self, record):
        # Format message
        msg = super(LogFormatter, self).format(record)

        # Add level name
        if record.levelno in [logging.INFO, PROGRESS]:
            with_level = msg
        else:
            with_level = "%s: %s" % (record.levelname, msg)

        # Add thread name
        if record.threadName == "MainThread":
            return with_level
        else:
            return "%s: %s" % (record.threadName, with_level)


class CollapsingLogHandler(logging.StreamHandler):
    def __init__(self, stream=sys.stdout):
        super(CollapsingLogHandler, self).__init__(stream)
        self.last_level = logging.INFO
        self.last_len = 0

    def emit(self, record):
        try:
            if self.last_level == PROGRESS:
                if record.levelno == PROGRESS:
                    self.stream.write("\r")
                else:
                    self.stream.write("\n")

            msg = self.format(record)
            if record.levelno == PROGRESS:
                self.stream.write(msg.ljust(self.last_len))
                self.last_len = max(len(msg), self.last_len)
            else:
                self.last_len = 0
                self.stream.write(msg)
                self.stream.write("\n")

            self.last_level = record.levelno
            self.flush()
        except Exception:
            self.handleError(record)


class TailLogHandler(logging.Handler):
    def __init__(self, capacity, max_level, flush_level, target_handler):
        super(TailLogHandler, self).__init__()
        self.buffer = collections.deque(maxlen=capacity)
        self.max_level = max_level
        self.flush_level = flush_level
        self.target_handler = target_handler

    def emit(self, record):
        if record.levelno < self.max_level:
            self.buffer.append(record)

        if record.levelno >= self.flush_level:
            while self.buffer:
                record = self.buffer.popleft()
                self.target_handler.handle(record)


class CensorLogFilter(logging.Filter):
    def __init__(self, keyword):
        self.keyword = keyword

    def censor(self, msg):
        if not isinstance(msg, str):
            return msg

        if self.keyword:
            return msg.replace(self.keyword, "*" * len(self.keyword))
        else:
            return msg

    def filter(self, record):
        record.msg = self.censor(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self.censor(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self.censor(value) for key, value in record.args.items()}
        return True


def setup_logging(verbosity, stream=sys.stdout):
    logger = logging.getLogger()
    logger.setLevel(ENGINE)

    handler = logging.StreamHandler(stream)

    if verbosity >= 3:
        handler.setLevel(ENGINE)
    elif verbosity >= 2:
        handler.setLevel(logging.DEBUG)
    elif verbosity >= 1:
        handler.setLevel(PROGRESS)
    else:
        if stream.isatty():
            handler = CollapsingLogHandler(stream)
            handler.setLevel(PROGRESS)
        else:
            handler.setLevel(logging.INFO)

    if verbosity < 2:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests.packages.urllib3").setLevel(logging.WARNING)

    tail_target = logging.StreamHandler(stream)
    tail_target.setFormatter(LogFormatter())
    logger.addHandler(TailLogHandler(35, handler.level, logging.ERROR, tail_target))

    handler.setFormatter(LogFormatter())
    logger.addHandler(handler)
