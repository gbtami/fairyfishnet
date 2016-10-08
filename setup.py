#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of the lichess.org fishnet client.
# Copyright (C) 2016 Niklas Fiekas <niklas.fiekas@backscattering.de>
# See LICENSE.txt for licensing information.

import fishnet
import setuptools
import os.path


def read_description():
    with open(os.path.join(os.path.dirname(__file__), "README.rst")) as readme:
        description = readme.read()

    # Show the Travis CI build status of the concrete version
    description = description.replace(
        "//travis-ci.org/niklasf/fishnet.svg?branch=master",
        "//travis-ci.org/niklasf/fishnet.svg?branch=v{0}".format(fishnet.__version__))

    return description


setuptools.setup(
    name="fishnet",
    version=fishnet.__version__,
    author=fishnet.__author__,
    author_email=fishnet.__email__,
    description=fishnet.__doc__.replace("\n", " ").strip(),
    long_description=read_description(),
    keywords="lichess.org chess stockfish uci",
    url="https://github.com/niklasf/fishnet",
    py_modules=["fishnet"],
    test_suite="test",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 2.7",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.3",
        "Programming Language :: Python :: 3.4",
        "Programming Language :: Python :: 3.5",
        "Topic :: Games/Entertainment :: Board Games",
        "Topic :: Internet :: WWW/HTTP",
    ]
)
