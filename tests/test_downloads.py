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
