import configparser
import hashlib
import multiprocessing
import tempfile
import unittest

import pytest

from fairyfishnet.config import get_stockfish_command
from fairyfishnet.engine import setoption
from fairyfishnet.variants import write_variants_ini
from fairyfishnet.worker import Worker

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.mark.engine
class WorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine_dir = tempfile.TemporaryDirectory()
        conf = configparser.ConfigParser()
        conf.add_section("Fishnet")
        conf.set("Fishnet", "EngineDir", cls.engine_dir.name)
        payload = "[fishnet-test:chess]\n"
        cls.variants_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        write_variants_ini(conf, payload, cls.variants_sha256)
        get_stockfish_command(conf, update=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine_dir.cleanup()

    def setUp(self):
        conf = configparser.ConfigParser()
        conf.add_section("Fishnet")
        conf.set("Fishnet", "Key", "testkey")
        conf.set("Fishnet", "EngineDir", self.engine_dir.name)
        self.worker = Worker(
            conf,
            threads=multiprocessing.cpu_count(),
            memory=32,
            progress_reporter=None,
        )
        self.worker.start_stockfish()

    def tearDown(self):
        self.worker.stop()

    def test_bestmove(self):
        job = {
            "work": {"type": "move", "id": "abcdefgh", "level": 8},
            "game_id": "hgfedcba",
            "variant": "chess",
            "variantsSha256": self.variants_sha256,
            "position": STARTPOS,
            "moves": "f2f3 e7e6 g2g4",
        }
        response = self.worker.bestmove(job)
        self.assertEqual(response["move"]["bestmove"], "d8h4")

    def test_zh_bestmove(self):
        job = {
            "work": {"type": "move", "id": "hihihihi", "level": 1},
            "game_id": "ihihihih",
            "variant": "crazyhouse",
            "variantsSha256": self.variants_sha256,
            "position": "rnbqk1nr/ppp2ppp/3b4/3N4/4p1PP/5P2/PPPPP3/R1BQKBNR[P] b KQkq - 9 5",
            "moves": "d6g3",
        }
        response = self.worker.bestmove(job)
        self.assertEqual(response["move"]["bestmove"], "P@f2")

    def test_analysis(self):
        job = {
            "work": {"type": "analysis", "id": "12345678"},
            "game_id": "87654321",
            "variant": "chess",
            "variantsSha256": self.variants_sha256,
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
        setoption(self.worker.stockfish, "Threads", 1)
        job = {
            "work": {"type": "analysis", "id": "contempt 100"},
            "variant": "chess",
            "variantsSha256": self.variants_sha256,
            "position": STARTPOS,
            "moves": "d2d4 d7d5",
            "skipPositions": [0, 1],
            "nodes": 1000,
        }
        setoption(self.worker.stockfish, "Contempt", 100)
        cp_100 = self.worker.analysis(job)["analysis"][2]["score"]["cp"]
        job["work"]["id"] = "contempt 0"
        setoption(self.worker.stockfish, "Contempt", 0)
        cp_0 = self.worker.analysis(job)["analysis"][2]["score"]["cp"]
        self.assertEqual(cp_100, cp_0)
