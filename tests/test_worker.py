import configparser

import fairyfishnet.worker as worker_module
from fairyfishnet.constants import ABORT_REASON_ENGINE_CRASH
from fairyfishnet.worker import Worker


def make_worker():
    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.set("Fishnet", "Key", "testkey")
    conf.set("Fishnet", "Endpoint", "https://www.pychess.org/fishnet/")
    return Worker(conf, threads=2, memory=64, progress_reporter=None)


def test_make_request_contains_worker_and_engine_metadata(monkeypatch):
    worker = make_worker()
    worker.stockfish_info = {"name": "Fairy-Stockfish", "options": {}}
    monkeypatch.setattr(worker_module.platform, "python_version", lambda: "3.8.20")
    request = worker.make_request()
    assert request["fishnet"]["apikey"] == "testkey"
    assert request["fishnet"]["python"] == "3.8.20"
    assert request["stockfish"]["name"] == "Fairy-Stockfish"


def test_job_name_uses_game_url_and_optional_ply():
    worker = make_worker()
    job = {"game_id": "abcdefgh", "work": {"id": "work-id"}}
    assert worker.job_name(job) == "https://www.pychess.org/abcdefgh"
    assert worker.job_name(job, 12) == "https://www.pychess.org/abcdefgh#12"


def test_job_name_falls_back_to_work_id():
    worker = make_worker()
    assert worker.job_name({"work": {"id": "work-id"}}) == "work-id"


def test_work_routes_analysis(monkeypatch):
    worker = make_worker()
    worker.job = {"work": {"type": "analysis", "id": "abc"}}
    monkeypatch.setattr(worker, "analysis", lambda job: {"analysis": []})
    assert worker.work() == ("analysis/abc", {"analysis": []})


def test_work_routes_move(monkeypatch):
    worker = make_worker()
    worker.job = {"work": {"type": "move", "id": "abc"}}
    monkeypatch.setattr(worker, "bestmove", lambda job: {"move": {}})
    assert worker.work() == ("move/abc", {"move": {}})


def test_worker_recovers_from_dead_engine_error():
    worker = object.__new__(Worker)
    worker.start_stockfish = lambda: (_ for _ in ()).throw(EOFError())
    worker.is_alive = lambda: False
    worker.stockfish = None
    aborted = []
    worker.abort_job = lambda error=None: aborted.append(error)
    worker.run_inner()
    assert aborted == [{"reason": ABORT_REASON_ENGINE_CRASH, "kind": "EOFError"}]
