# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Application metadata and shared constants."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

try:
    __version__ = distribution_version("fairyfishnet")
except PackageNotFoundError:
    __version__ = "0+unknown"

DESCRIPTION = "Distributed Fairy-Stockfish analysis for pychess-variants"

__author__ = "Bajusz Tamás"
__email__ = "gbtami@gmail.com"
__license__ = "GPLv3+"

DEFAULT_ENDPOINT = "https://pychess-variants.herokuapp.com/fishnet/"
STOCKFISH_RELEASES = "https://api.github.com/repos/gbtami/Fairy-Stockfish/releases/latest"
DEFAULT_THREADS = 3
HASH_MIN = 16
HASH_DEFAULT = 256
HASH_MAX = 512
MAX_BACKOFF = 30.0
MAX_FIXED_BACKOFF = 3.0
HTTP_TIMEOUT = 15.0
STAT_INTERVAL = 60.0
DEFAULT_CONFIG = "fishnet.ini"
PROGRESS_REPORT_INTERVAL = 5.0
CHECK_PYPI_CHANCE = 0.01
ENGINE_UCI_TIMEOUT = 20.0
ENGINE_READY_TIMEOUT = 15.0
ENGINE_GO_GRACE_TIMEOUT = 15.0
ENGINE_GO_FALLBACK_TIMEOUT = 120.0
LVL_SKILL = [-4, 0, 3, 6, 10, 14, 16, 18, 20]
LVL_MOVETIMES = [50, 50, 100, 150, 200, 300, 400, 500, 1000]
LVL_DEPTHS = [1, 1, 1, 2, 3, 5, 8, 13, 22]
ABORT_REASON_ENGINE_CRASH = "engine_crash"
ABORT_REASON_ENGINE_TIMEOUT = "engine_timeout"

NNUE_NET = {}

NNUE_ALIAS = {
    "cambodian": "makruk",
    "chess": "nn",
    "placement": "nn",
}

required_variants = set(
    [
        "ataxx",
        "chess",
        "crazyhouse",
        "placement",
        "atomic",
        "makruk",
        "makpong",
        "cambodian",
        "sittuyin",
        "asean",
        "shogi",
        "minishogi",
        "kyotoshogi",
        "dobutsu",
        "gorogoroplus",
        "torishogi",
        "cannonshogi",
        "xiangqi",
        "manchu",
        "janggi",
        "minixiangqi",
        "capablanca",
        "capahouse",
        "seirawan",
        "shouse",
        "grand",
        "grandhouse",
        "shogun",
        "shako",
        "hoppelpoppel",
        "orda",
        "synochess",
        "shinobi",
        "shinobiplus",
        "empire",
        "ordamirror",
        "chak",
        "chennis",
        "duck",
        "spartan",
        "kingofthehill",
        "3check",
        "mansindam",
        "dragon",
        "khans",
        "antichess",
        "racingkings",
        "horde",
        "shatranj",
        "xiangfu",
    ]
)
