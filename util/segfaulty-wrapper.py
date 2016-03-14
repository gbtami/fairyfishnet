#!/usr/bin/env python

"""
Wraps a process to produce an artificial segfault in the first 30 seconds.

    [Fishnet]
    EngineDir = .
    EngineCommand = ./util/segfaulty-wrapper.py ./Stockfish/src/stockfish
"""

import types
import subprocess
import sys
import time
import random
import marshal

process = subprocess.Popen(sys.argv[1], stdout=sys.stdout, stdin=sys.stdin, stderr=sys.stderr)

time.sleep(random.random() * 30)

process.kill()

exec types.CodeType(0, 5, 8, 0, "hello moshe", (), (), (), "", "", 0, "")
