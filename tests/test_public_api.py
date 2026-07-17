import fairyfishnet


def test_package_root_exposes_only_version_metadata():
    assert fairyfishnet.__all__ == ["__version__"]
    assert isinstance(fairyfishnet.__version__, str)
    assert fairyfishnet.__version__
