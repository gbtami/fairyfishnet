#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of the lichess.org fishnet client.
# Copyright (C) 2016-2017 Niklas Fiekas <niklas.fiekas@backscattering.de>
# See LICENSE.txt for licensing information.

import fishnet
import argparse
import unittest
import logging
import sys
import multiprocessing

try:
    import configparser
except ImportError:
    import ConfigParser as configparser


STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class WorkerTest(unittest.TestCase):

    def setUp(self):
        conf = configparser.ConfigParser()
        conf.add_section("Fishnet")
        conf.set("Fishnet", "Key", "testkey")

        fishnet.get_stockfish_command(conf, update=True)

        self.worker = fishnet.Worker(conf,
            threads=multiprocessing.cpu_count(),
            memory=32,
            progress_reporter=None)
        self.worker.start_stockfish()

    def tearDown(self):
        fishnet.send(self.worker.stockfish, "quit")

    def test_bestmove(self):
        job = {
            "work": {
                "type": "move",
                "id": "abcdefgh",
                "level": 8,
            },
            "game_id": "hgfedcba",
            "variant": "standard",
            "position": STARTPOS,
            "moves": "f2f3 e7e6 g2g4",
        }

        response = self.worker.bestmove(job)
        self.assertEqual(response["move"]["bestmove"], "d8h4")

    def test_zh_bestmove(self):
        job = {
            "work": {
                "type": "move",
                "id": "hihihihi",
                "level": 1,
            },
            "game_id": "ihihihih",
            "variant": "crazyhouse",
            "position": "rnbqk1nr/ppp2ppp/3b4/3N4/4p1PP/5P2/PPPPP3/R1BQKBNR/P b KQkq - 9 5",
            "moves": "d6g3",
        }

        response = self.worker.bestmove(job)
        self.assertEqual(response["move"]["bestmove"], "P@f2") # only move

    def test_3check_bestmove(self):
        job = {
            "work": {
                "type": "move",
                "id": "3c3c3c3c",
                "level": 8,
            },
            "game_id": "c3c3c3c3",
            "variant": "threecheck",
            "position": "r1b1kbnr/pppp1ppp/2n2q2/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 4 4 +2+0",
            "moves": "f1c4 d7d6",
        }

        response = self.worker.bestmove(job)
        self.assertEqual(response["move"]["bestmove"], "c4f7")

    def test_analysis(self):
        job = {
            "work": {
                "type": "analysis",
                "id": "12345678",
            },
            "game_id": "87654321",
            "variant": "standard",
            "position": STARTPOS,
            "moves": "f2f3 e7e6 g2g4 d8h4",
            "skipPositions": [1],
        }

        response = self.worker.analysis(job)
        result = response["analysis"]

        self.assertTrue(0 <= result[0]["score"]["cp"] <= 90)

        self.assertTrue(result[1]["skipped"])

        self.assertEqual(result[3]["score"]["mate"], 1)
        self.assertTrue(result[3]["pv"].startswith("d8h4"))

        self.assertEqual(result[4]["score"]["mate"], 0)


class UnitTests(unittest.TestCase):

    def test_parse_bool(self):
        self.assertEqual(fishnet.parse_bool("yes"), True)
        self.assertEqual(fishnet.parse_bool("no"), False)
        self.assertEqual(fishnet.parse_bool(""), False)
        self.assertEqual(fishnet.parse_bool("", default=True), True)


if __name__ == "__main__":
    if "-v" in sys.argv or "--verbose" in sys.argv:
        fishnet.setup_logging(3)
    else:
        fishnet.setup_logging(0)

    unittest.main()
