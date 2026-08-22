import logging

import pytest

import fairyfishnet.downloads as downloads


def test_stockfish_filename_linux_modern(monkeypatch):
    monkeypatch.setattr(downloads.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(downloads, "detect_cpu_capabilities", lambda: ("AuthenticAMD", True, False))
    monkeypatch.setattr(downloads.os, "name", "posix")
    monkeypatch.setattr(downloads.sys, "platform", "linux")
    assert downloads.stockfish_filename() == "stockfish-x86_64-modern"


def test_stockfish_filename_linux_bmi2_for_intel(monkeypatch):
    monkeypatch.setattr(downloads.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(downloads, "detect_cpu_capabilities", lambda: ("GenuineIntel", True, True))
    monkeypatch.setattr(downloads.os, "name", "posix")
    monkeypatch.setattr(downloads.sys, "platform", "linux")
    assert downloads.stockfish_filename() == "stockfish-x86_64-bmi2"


def test_stockfish_filename_windows(monkeypatch):
    monkeypatch.setattr(downloads.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(downloads, "detect_cpu_capabilities", lambda: ("", False, False))
    monkeypatch.setattr(downloads.os, "name", "nt")
    assert downloads.stockfish_filename() == "stockfish-windows-amd64.exe"


def test_stockfish_filename_macos_apple_silicon_skips_cpuid(monkeypatch):
    monkeypatch.setattr(downloads.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(downloads.sys, "platform", "darwin")
    monkeypatch.setattr(
        downloads,
        "detect_cpu_capabilities",
        lambda: (_ for _ in ()).throw(AssertionError("macOS must not run CPUID")),
    )
    assert downloads.stockfish_filename() == "stockfish-osx-arm64"


def test_stockfish_filename_macos_x86_64_skips_unused_cpuid(monkeypatch):
    monkeypatch.setattr(downloads.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(downloads.sys, "platform", "darwin")
    monkeypatch.setattr(
        downloads,
        "detect_cpu_capabilities",
        lambda: (_ for _ in ()).throw(AssertionError("macOS filenames do not use CPUID features")),
    )
    assert downloads.stockfish_filename() == "stockfish-osx-x86_64"


def test_stockfish_filename_linux_arm_skips_cpuid(monkeypatch):
    monkeypatch.setattr(downloads.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(downloads.os, "name", "posix")
    monkeypatch.setattr(downloads.sys, "platform", "linux")
    monkeypatch.setattr(
        downloads,
        "detect_cpu_capabilities",
        lambda: (_ for _ in ()).throw(AssertionError("non-x86 platforms must not run CPUID")),
    )
    assert downloads.stockfish_filename() == "stockfish-aarch64"


def test_respawn_self_uses_execv_on_posix(monkeypatch):
    executable = "/opt/Fairy Fishnet/bin/python"
    arguments = ["fairyfishnet", "--conf", "/srv/Fairy Fishnet/fishnet.ini", "run"]
    expected = [executable, "-m", "fairyfishnet"] + arguments[1:]

    monkeypatch.setattr(downloads.os, "name", "posix")
    monkeypatch.setattr(downloads.sys, "executable", executable)
    monkeypatch.setattr(downloads.sys, "argv", arguments)
    monkeypatch.setattr(
        downloads.subprocess,
        "call",
        lambda *args, **kwargs: pytest.fail("POSIX respawn must not start a child process"),
    )

    def execv(path, argv):
        assert path == executable
        assert argv == expected
        raise RuntimeError("execv called")

    monkeypatch.setattr(downloads.os, "execv", execv)

    with pytest.raises(RuntimeError, match="execv called"):
        downloads._respawn_self()


def test_respawn_self_waits_on_windows_and_preserves_spaced_arguments(monkeypatch, caplog):
    executable = r"C:\Program Files\Python\python.exe"
    working_directory = r"C:\Fairy Fishnet Worker"
    arguments = [
        r"C:\Python Scripts\fairyfishnet.exe",
        "--conf",
        r"C:\Fairy Fishnet Worker\fishnet.ini",
        "--key",
        "secret-api-key",
        "run",
    ]
    expected = [executable, "-m", "fairyfishnet"] + arguments[1:]
    calls = []

    monkeypatch.setattr(downloads.os, "name", "nt")
    monkeypatch.setattr(downloads.os, "getcwd", lambda: working_directory)
    monkeypatch.setattr(downloads.os, "execv", lambda *args: pytest.fail("Windows respawn must wait for a child"))
    monkeypatch.setattr(downloads.sys, "executable", executable)
    monkeypatch.setattr(downloads.sys, "argv", arguments)

    def call(argv, **kwargs):
        calls.append((argv, kwargs))
        return 23

    monkeypatch.setattr(downloads.subprocess, "call", call)

    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as exc_info:
        downloads._respawn_self()

    assert exc_info.value.code == 23
    assert calls == [(expected, {"executable": executable, "cwd": working_directory})]
    assert "secret-api-key" not in caplog.text


@pytest.mark.skipif(downloads.os.name != "nt", reason="requires Windows process creation")
def test_replace_process_waits_and_preserves_spaced_arguments_on_windows(tmp_path, monkeypatch):
    working_directory = tmp_path / "working directory"
    working_directory.mkdir()
    result_file = working_directory / "result with spaces.txt"
    spaced_argument = "argument with spaces"
    child_code = (
        "import os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(os.getcwd() + '\\n' + sys.argv[2], encoding='utf-8'); "
        "raise SystemExit(17)"
    )
    argv = [downloads.sys.executable, "-c", child_code, str(result_file), spaced_argument]

    monkeypatch.chdir(working_directory)

    with pytest.raises(SystemExit) as exc_info:
        downloads._replace_process(downloads.sys.executable, argv)

    assert exc_info.value.code == 17
    child_cwd, child_argument = result_file.read_text(encoding="utf-8").splitlines()
    assert downloads.os.path.normcase(child_cwd) == downloads.os.path.normcase(str(working_directory))
    assert child_argument == spaced_argument
