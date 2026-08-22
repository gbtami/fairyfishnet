# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Engine download and fairyfishnet self-update helpers."""

import logging
import os
import platform
import random
import site
import stat
import string
import struct
import subprocess
import sys
import time

from .config import get_engine_dir
from .constants import HTTP_TIMEOUT, STOCKFISH_RELEASES, __version__
from .dependencies import requests
from .engine import open_process
from .errors import ConfigError
from .http_utils import is_newer_version, release_file_url, response_json

X86_MACHINES = frozenset(("amd64", "x86_64", "x86", "i386", "i686"))


def detect_cpu_capabilities():
    # Detects support for popcnt and pext instructions
    vendor, modern, bmi2 = "", False, False

    # Run cpuid in subprocess for robustness in case of segfaults
    cmd = []
    cmd.append(sys.executable)
    if __package__ is not None:
        cmd.extend(["-m", "fairyfishnet"])
    else:
        cmd.append(__file__)
    cmd.append("cpuid")

    process = open_process(cmd, shell=False)
    assert process.stdout is not None

    # Parse output
    while True:
        line = process.stdout.readline()
        if not line:
            break

        line = line.rstrip()
        logging.debug("cpuid >> %s", line)
        if not line:
            continue

        columns = line.split()
        if columns[0] == "CPUID":
            pass
        elif len(columns) == 5 and all(all(c in string.hexdigits for c in col) for col in columns):
            eax, a, b, c, d = [int(col, 16) for col in columns]

            # vendor
            if eax == 0:
                vendor = struct.pack("III", b, d, c).decode("utf-8")

            # popcnt
            if eax == 1 and c & (1 << 23):
                modern = True

            # pext
            if eax == 7 and b & (1 << 8):
                bmi2 = True
        else:
            logging.warning("Unexpected cpuid output: %s", line)

    # Done
    process.communicate()
    if process.returncode != 0:
        logging.error("cpuid exited with status code %d", process.returncode)

    return vendor, modern, bmi2


def stockfish_filename():
    machine = platform.machine().lower()

    # macOS binaries do not use x86 feature suffixes, and CPUID is not
    # available on ARM or other non-x86 architectures.
    if os.name == "os2" or sys.platform == "darwin":
        return "stockfish-osx-%s" % machine

    suffix = ""
    if machine in X86_MACHINES:
        vendor, modern, bmi2 = detect_cpu_capabilities()
        if modern and "Intel" in vendor and bmi2:
            suffix = "-bmi2"
        elif modern:
            suffix = "-modern"

    if os.name == "nt":
        return "stockfish-windows-%s%s.exe" % (machine, suffix)
    elif os.name == "posix":
        return "stockfish-%s%s" % (machine, suffix)


def download_github_release(conf, release_page, filename):
    path = os.path.join(get_engine_dir(conf), filename)
    logging.info("Engine target path: %s", path)

    headers = {}
    headers["User-Agent"] = "fairyfishnet"

    # Only update to newer versions
    try:
        headers["If-Modified-Since"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(os.path.getmtime(path)))
    except OSError:
        pass

    # Escape GitHub API rate limiting
    if "GITHUB_API_TOKEN" in os.environ:
        headers["Authorization"] = "token %s" % os.environ["GITHUB_API_TOKEN"]

    # Find latest release
    logging.info("Looking up %s ...", filename)

    response = requests.get(release_page, headers=headers, timeout=HTTP_TIMEOUT)
    if response.status_code == 304:
        logging.info("Local %s is newer than release", filename)
        return filename
    elif response.status_code != 200:
        raise ConfigError("Failed to look up latest Stockfish release (status %d)" % (response.status_code,))

    release = response_json(response, "GitHub release lookup")

    logging.info("Latest release is tagged %s", release["tag_name"])

    for asset in release["assets"]:
        if asset["name"] == filename:
            logging.info("Found %s" % asset["browser_download_url"])
            break
    else:
        raise ConfigError("No precompiled %s for your platform" % filename)

    # Download
    logging.info("Downloading %s ...", filename)

    download = requests.get(asset["browser_download_url"], stream=True, timeout=HTTP_TIMEOUT)
    progress = 0
    size = int(download.headers["content-length"])
    with open(path, "wb") as target:
        for chunk in download.iter_content(chunk_size=1024):
            target.write(chunk)
            progress += len(chunk)

            if sys.stderr.isatty():
                sys.stderr.write("\rDownloading %s: %d/%d (%d%%)" % (filename, progress, size, progress * 100 / size))
                sys.stderr.flush()
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()

    # Make executable
    logging.info("chmod +x %s", filename)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC)
    return filename


def update_stockfish(conf, filename):
    return download_github_release(conf, STOCKFISH_RELEASES, filename)


def is_user_site_package():
    try:
        user_site = site.getusersitepackages()
    except AttributeError:
        return False

    return os.path.abspath(__file__).startswith(os.path.join(user_site, ""))


def update_self():
    # Ensure current instance is installed as a package
    if __package__ is None:
        raise ConfigError("Not started as a package (python -m). Cannot update using pip")

    if all(dirname not in ["site-packages", "dist-packages"] for dirname in __file__.split(os.sep)):
        raise ConfigError("Not installed as package (%s). Cannot update using pip" % __file__)

    logging.debug(
        'Package: "%s", name: %s, loader: %s',
        __package__,
        __name__,
        __loader__,
    )

    # Ensure pip is available
    try:
        pip_info = subprocess.check_output(
            [sys.executable, "-m", "pip", "--version"],
            universal_newlines=True,
        )
    except OSError:
        raise ConfigError("Auto update enabled, but cannot run pip")
    else:
        logging.debug("Pip: %s", pip_info.rstrip())

    # Ensure module file is going to be writable
    try:
        with open(__file__, "r+"):
            pass
    except IOError:
        raise ConfigError(
            "Auto update enabled, but no write permissions to module file. Use virtualenv or pip install --user"
        )

    # Look up the latest version
    response = requests.get("https://pypi.org/pypi/fairyfishnet/json", timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    result = response_json(response, "PyPI fairyfishnet metadata")
    latest_version = result["info"]["version"]
    if latest_version == __version__:
        logging.info("Already up to date.")
        return 0
    if not is_newer_version(latest_version, __version__):
        logging.info(
            "Ignoring PyPI fairyfishnet version %s because local version %s is newer",
            latest_version,
            __version__,
        )
        return 0

    url = release_file_url(result["releases"][latest_version])

    # Wait
    t = random.random() * 15.0
    logging.info("Waiting %0.1fs before update ...", t)
    time.sleep(t)
    print()

    # Update
    if is_user_site_package():
        logging.info("$ pip install --user --upgrade %s", url)
        ret = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--user", "--upgrade", url],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    else:
        logging.info("$ pip install --upgrade %s", url)
        ret = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--upgrade", url],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    if ret != 0:
        logging.warning("Unexpected exit code for pip install: %d", ret)
        return ret

    print()

    # Wait
    t = random.random() * 15.0
    logging.info("Waiting %0.1fs before respawn ...", t)
    time.sleep(t)

    # Respawn through the stable package name after moving to a package layout.
    argv = [sys.executable, "-m", "fairyfishnet"] + sys.argv[1:]

    logging.debug("Restarting with execv: %s, argv: %s", sys.executable, " ".join(argv))

    os.execv(sys.executable, argv)


def update_available():
    try:
        response = requests.get("https://pypi.org/pypi/fairyfishnet/json", timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        result = response_json(response, "PyPI fairyfishnet metadata")
        latest_version = result["info"]["version"]
    except Exception:
        logging.exception("Failed to check for update on PyPI")
        return False

    if latest_version == __version__:
        logging.info("[fairyfishnet v%s] Client is up to date", __version__)
        return False
    if not is_newer_version(latest_version, __version__):
        logging.info(
            "[fairyfishnet v%s] Ignoring older PyPI version: %s",
            __version__,
            latest_version,
        )
        return False

    logging.info("[fairyfishnet v%s] Update available on PyPI: %s", __version__, latest_version)
    return True
