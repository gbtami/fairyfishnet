#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of the lichess.org fishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# See LICENSE.txt for licensing information.

import argparse
import configparser
import multiprocessing
import os
import sys
import tempfile
import time
import unittest

import pytest

import fairyfishnet

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.mark.engine
class WorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine_dir = tempfile.TemporaryDirectory()
        conf = configparser.ConfigParser()
        conf.add_section("Fishnet")
        conf.set("Fishnet", "EngineDir", cls.engine_dir.name)

        args = argparse.Namespace(
            no_conf=True,
            conf=None,
            engine_dir=cls.engine_dir.name,
            setoption=[],
        )
        fairyfishnet.create_variants_ini(args)
        fairyfishnet.get_stockfish_command(conf, update=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine_dir.cleanup()

    def setUp(self):
        conf = configparser.ConfigParser()
        conf.add_section("Fishnet")
        conf.set("Fishnet", "Key", "testkey")
        conf.set("Fishnet", "EngineDir", self.engine_dir.name)

        self.worker = fairyfishnet.Worker(conf, threads=multiprocessing.cpu_count(), memory=32, progress_reporter=None)
        self.worker.start_stockfish()

    def tearDown(self):
        self.worker.stop()

    def test_bestmove(self):
        job = {
            "work": {
                "type": "move",
                "id": "abcdefgh",
                "level": 8,
            },
            "game_id": "hgfedcba",
            "variant": "chess",
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
            "position": "rnbqk1nr/ppp2ppp/3b4/3N4/4p1PP/5P2/PPPPP3/R1BQKBNR[P] b KQkq - 9 5",
            "moves": "d6g3",
        }

        response = self.worker.bestmove(job)
        self.assertEqual(response["move"]["bestmove"], "P@f2")  # only move

    def xxxtest_3check_bestmove(self):
        job = {
            "work": {
                "type": "move",
                "id": "3c3c3c3c",
                "level": 8,
            },
            "game_id": "c3c3c3c3",
            "variant": "3check",
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
            "variant": "chess",
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

    def test_analysis_contempt(self):
        fairyfishnet.setoption(self.worker.stockfish, "Threads", 1)

        job = {
            "work": {
                "type": "analysis",
                "id": "contempt 100",
            },
            "variant": "chess",
            "position": STARTPOS,
            "moves": "d2d4 d7d5",
            "skipPositions": [0, 1],
            "nodes": 1000,
        }

        fairyfishnet.setoption(self.worker.stockfish, "Contempt", 100)

        response = self.worker.analysis(job)
        cp_100 = response["analysis"][2]["score"]["cp"]

        job["work"]["id"] = "contempt 0"
        fairyfishnet.setoption(self.worker.stockfish, "Contempt", 0)
        response = self.worker.analysis(job)
        cp_0 = response["analysis"][2]["score"]["cp"]

        self.assertEqual(cp_100, cp_0)


class UnitTests(unittest.TestCase):
    def variants_conf(self, engine_dir):
        conf = configparser.ConfigParser()
        conf.add_section("Fishnet")
        conf.set("Fishnet", "EngineDir", engine_dir)
        return conf

    def test_parse_bool(self):
        self.assertEqual(fairyfishnet.parse_bool("yes"), True)
        self.assertEqual(fairyfishnet.parse_bool("no"), False)
        self.assertEqual(fairyfishnet.parse_bool(""), False)
        self.assertEqual(fairyfishnet.parse_bool("", default=True), True)

    def test_reload_engine_variants_uses_selected_cache_entry(self):
        with tempfile.TemporaryDirectory() as engine_dir:
            conf = self.variants_conf(engine_dir)
            sha256 = "a" * 64
            entry = fairyfishnet.write_variants_ini(conf, "[custom]\n", sha256=sha256, scoped=True)
            calls = []
            original_setoption = fairyfishnet.setoption
            original_isready = fairyfishnet.isready
            try:
                fairyfishnet.setoption = lambda process, name, value: calls.append((name, value))
                fairyfishnet.isready = lambda process: None
                selected = fairyfishnet.reload_engine_variants(object(), conf, expected_sha256=sha256)
            finally:
                fairyfishnet.setoption = original_setoption
                fairyfishnet.isready = original_isready

            self.assertEqual(selected, entry)
            self.assertEqual(calls, [("VariantPath", entry.filename)])

    def test_cleanup_preserves_active_variants_ini(self):
        with tempfile.TemporaryDirectory() as engine_dir:
            conf = self.variants_conf(engine_dir)
            now = time.time()
            active = fairyfishnet.write_variants_ini(conf, "[active]\n", sha256="a" * 64, scoped=True)
            inactive = fairyfishnet.write_variants_ini(conf, "[inactive]\n", sha256="b" * 64, scoped=True)
            os.utime(active.path, (now - 100, now - 100))
            os.utime(inactive.path, (now - 100, now - 100))

            with fairyfishnet.active_variants_ini(conf, active):
                deleted = fairyfishnet.cleanup_variants_ini_cache(conf, now=now, max_files=0, min_age=0)
                self.assertTrue(os.path.exists(active.path))
                self.assertFalse(os.path.exists(inactive.path))

            self.assertEqual(deleted, 1)

    def test_cleanup_keeps_newest_cache_entries(self):
        with tempfile.TemporaryDirectory() as engine_dir:
            conf = self.variants_conf(engine_dir)
            now = time.time()
            older = fairyfishnet.write_variants_ini(conf, "[older]\n", sha256="e" * 64, scoped=True)
            newer = fairyfishnet.write_variants_ini(conf, "[newer]\n", sha256="f" * 64, scoped=True)
            os.utime(older.path, (now - 200, now - 200))
            os.utime(newer.path, (now - 100, now - 100))

            deleted = fairyfishnet.cleanup_variants_ini_cache(conf, now=now, max_files=1, min_age=0)

            self.assertEqual(deleted, 1)
            self.assertFalse(os.path.exists(older.path))
            self.assertTrue(os.path.exists(newer.path))

    def test_cleanup_keeps_recent_and_unscoped_files(self):
        with tempfile.TemporaryDirectory() as engine_dir:
            conf = self.variants_conf(engine_dir)
            now = time.time()
            recent = fairyfishnet.write_variants_ini(conf, "[recent]\n", sha256="c" * 64, scoped=True)
            unscoped = fairyfishnet.write_variants_ini(conf, "[base]\n")

            deleted = fairyfishnet.cleanup_variants_ini_cache(conf, now=now, max_files=0, min_age=60)

            self.assertEqual(deleted, 0)
            self.assertTrue(os.path.exists(recent.path))
            self.assertTrue(os.path.exists(unscoped.path))

    def test_worker_recovers_from_dead_engine_error(self):
        worker = object.__new__(fairyfishnet.Worker)
        worker.start_stockfish = lambda: (_ for _ in ()).throw(EOFError())
        worker.is_alive = lambda: False
        worker.stockfish = None
        aborted = []
        worker.abort_job = lambda error=None: aborted.append(error)

        worker.run_inner()

        self.assertEqual(
            aborted,
            [{"reason": fairyfishnet.ABORT_REASON_ENGINE_CRASH, "kind": "EOFError"}],
        )


if __name__ == "__main__":
    if "-v" in sys.argv or "--verbose" in sys.argv:
        fairyfishnet.setup_logging(3)
    else:
        fairyfishnet.setup_logging(0)

    unittest.main()
