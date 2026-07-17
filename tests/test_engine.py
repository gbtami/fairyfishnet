import fairyfishnet.engine as engine


def test_file_of_counts_multi_digit_empty_squares():
    assert engine.file_of("K", "3p2K") == 6
    assert engine.file_of("K", "8") == -1


def test_modded_variant_uses_embassy_for_e_file_kings():
    fen = "rnbqkacbnr/pppppppppp/10/10/10/10/PPPPPPPPPP/RNBQKACBNR w KQkq - 0 1"
    assert engine.modded_variant("capablanca", False, fen) == "embassy"
    assert engine.modded_variant("capahouse", False, fen) == "embassyhouse"


def test_modded_variant_keeps_original_for_chess960():
    fen = "rnbqkacbnr/pppppppppp/10/10/10/10/PPPPPPPPPP/RNBQKACBNR w KQkq - 0 1"
    assert engine.modded_variant("capablanca", True, fen) == "capablanca"


def test_modded_variant_keeps_original_without_castling_rights():
    fen = "rnbqkacbnr/pppppppppp/10/10/10/10/PPPPPPPPPP/RNBQKACBNR w - - 0 1"
    assert engine.modded_variant("capablanca", False, fen) == "capablanca"


def test_set_variant_options_maps_standard_aliases(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "setoption", lambda process, name, value: calls.append((name, value)))
    engine.set_variant_options(object(), "standard", False, False)
    assert calls == [("UCI_Chess960", False), ("UCI_Variant", "chess")]


def test_set_variant_options_uses_variant_name(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "setoption", lambda process, name, value: calls.append((name, value)))
    engine.set_variant_options(object(), "CrazyHouse", True, False)
    assert calls == [("UCI_Chess960", True), ("UCI_Variant", "crazyhouse")]
