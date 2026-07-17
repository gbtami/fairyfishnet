# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exceptions shared across fairyfishnet modules."""

DEAD_ENGINE_ERRORS = (EOFError, IOError, BrokenPipeError)


class EngineTimeout(Exception):
    pass


class ConfigError(Exception):
    pass


class VariantsIniError(Exception):
    pass


class JsonResponseError(Exception):
    pass


class UpdateRequired(Exception):
    pass


class Shutdown(Exception):
    pass


class ShutdownSoon(Exception):
    pass
