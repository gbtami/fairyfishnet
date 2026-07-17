# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Third-party dependency imports shared by the worker modules."""

import sys

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    print("fishnet requires the 'requests' module.", file=sys.stderr)
    print("Try 'pip install requests' or install python-requests from your distro packages.", file=sys.stderr)
    print(file=sys.stderr)
    raise

try:
    import pyffish as sf

    sf_ok = True
    if "cpuid" not in sys.argv[1:]:
        try:
            print(sf.version())
        except Exception:
            print("fairyfishnet requires pyffish", file=sys.stderr)
            raise
except ImportError:
    print("No pyffish module installed!", file=sys.stderr)
    sf_ok = False
    raise

__all__ = ["HTTPAdapter", "requests", "sf", "sf_ok"]
