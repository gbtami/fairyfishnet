#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# See LICENSE.txt for licensing information.

import setuptools
import re
import os.path


with open(os.path.join(os.path.dirname(__file__), "fairyfishnet.py"), "rb") as f:
    # Trick: Strip imports of dependencies
    fishnet = {}
    code = f.read().decode("utf-8")
    stripped_code = re.sub(r"^(\s*)(import requests\s*$)", r"\1pass", code, flags=re.MULTILINE).encode("utf-8")
    eval(compile(stripped_code, "fairyfishnet.py", "exec"), fishnet)


def read_description():
    with open(os.path.join(os.path.dirname(__file__), "README.rst")) as readme:
        description = readme.read()

    # Show the Travis CI build status of the concrete version
    description = description.replace(
        "//travis-ci.org/niklasf/fishnet.svg?branch=master",
        "//travis-ci.org/niklasf/fishnet.svg?branch=v{0}".format(fishnet["__version__"]))

    return description


setuptools.setup(
    name="fairyfishnet",
    version=fishnet["__version__"],
    author=fishnet["__author__"],
    author_email=fishnet["__email__"],
    description=fishnet["__doc__"].replace("\n", " ").strip(),
    long_description=read_description(),
    long_description_content_type="text/x-rst",
    keywords="lichess.org chess stockfish uci",
    url="https://github.com/gbtami/fairyfishnet",
    py_modules=["fairyfishnet"],
    test_suite="test",
    install_requires=[
        "requests==2.32.2",
        "pyffish==0.0.82",
        "gdown==5.1.0",
        "beautifulsoup4==4.12.3",
    ],
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Games/Entertainment :: Board Games",
        "Topic :: Internet :: WWW/HTTP",
    ]
)
