# Engine development notes

This document records the invariants that contributors and coding agents should preserve when changing Fairy-Stockfish integration. Read it together with `doc/protocol.md` before editing worker, UCI, dynamic-variant, or update code.

## Compatibility floor

Python 3.8 is the oldest supported interpreter.

Do not introduce syntax or standard-library APIs that require Python 3.9 or newer. In particular, avoid built-in generic annotations such as `list[str]`, PEP 604 unions such as `A | B`, structural pattern matching, and `tomllib` without a compatibility dependency.

Ruff and Pyright are both configured to evaluate the source as Python 3.8.

## Source map

- `src/fairyfishnet/__init__.py` contains the worker implementation, protocol client, UCI helpers, engine download logic, dynamic variant cache, and CLI.
- `src/fairyfishnet/__main__.py` supports `python -m fairyfishnet`.
- `tests/test_fairyfishnet.py` contains both fast unit tests and slower engine-backed tests.
- `build-stockfish.sh` builds the project-specific Fairy-Stockfish binary.
- `doc/protocol.md` documents communication with the pychess server.

The package remains intentionally monolithic for now. Large refactors should first extract well-defined boundaries without changing protocol behavior.

## Engine process invariants

Each `Worker` owns its Fairy-Stockfish subprocess. Never share a subprocess between worker threads.

All UCI commands must preserve request/response ordering. A command that changes options or variants must complete an `isready` round trip before analysis or move generation continues.

EOF, broken-pipe, and timeout paths must abort the current job with a structured reason and restart the engine rather than terminating the worker thread.

CPU capability detection launches `python -m fairyfishnet cpuid` as a subprocess. The `cpuid` command must not perform configuration loading, network access, engine startup, or ordinary logging before writing its machine-readable rows.

## Dynamic variant files

Server-provided variant definitions are content-addressed as `variants-<sha256>.ini` in `EngineDir`.

Preserve these rules:

1. Validate the expected SHA-256 before selecting a cached file.
2. Write new files atomically through a temporary file and `os.replace`.
3. Pass the selected filename directly to the worker that requested it; do not rely on a mutable process-global current filename.
4. Serialize pyffish `VariantPath` changes because pyffish stores that option globally in-process.
5. Mark an entry active for the complete engine job and publish the process lease before it can be cleaned.
6. Cleanup may delete only exact scoped-cache filenames that are old, outside the retained newest set, and absent from all live leases.
7. Never delete the unscoped `variants.ini` or unrelated `.ini` files.

Any cache-policy change needs tests for active entries, recent entries, retained newest entries, and unrelated files.

## Engine downloads and NNUE

Engine and network downloads are untrusted I/O. Keep explicit HTTP timeouts, status validation, temporary/atomic replacement where practical, and clear errors containing the failed resource.

Variant aliases used for NNUE selection must remain compatible with the names sent by pychess and accepted by Fairy-Stockfish. Test canonical variants and at least one alias when changing this mapping.

## Updates and packaging

The installed command is `fairyfishnet`; `python -m fairyfishnet` must remain equivalent because subprocess detection, systemd units, and respawning rely on it.

Self-update intentionally retains the established pip-based behavior:

```console
<current-interpreter> -m pip install [--user] --upgrade <release-url>
```

Development, locking, testing, and publishing use uv, but changing the worker's updater would be a runtime behavior change. After installation, the worker respawns with the same interpreter and `-m fairyfishnet`. When installing as an isolated uv tool, include pip with `uv tool install --with pip fairyfishnet`.

The Dockerfile uses uv's pip-compatible interface to install the latest released `fairyfishnet` package from PyPI into the container's system Python. It must not copy and install the repository checkout unless that deployment policy is changed deliberately.

Package metadata has a single version source in `pyproject.toml`. Runtime code obtains it from installed distribution metadata; do not restore build-time imports of the worker module.

## Validation checklist

Run all of the following before committing engine-related changes:

```console
uv run pytest -m "not engine"
uv run pytest -m engine
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv lock --check --python 3.8
uv build
```

For concurrency or cache work, also reason explicitly about:

- multiple worker threads in one process;
- multiple fairyfishnet processes sharing an `EngineDir`;
- process termination while a job or atomic write is in progress;
- a server retry delivering the same variant hash;
- switching back from a scoped variant file to the bundled `variants.ini`.
