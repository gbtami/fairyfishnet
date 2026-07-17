# Engine development notes

This document records the invariants that contributors and coding agents should preserve when changing Fairy-Stockfish integration. Read it together with `doc/protocol.md` before editing worker, UCI, dynamic-variant, or update code.

## Compatibility floor

Python 3.8 is the oldest supported interpreter.

Do not introduce syntax or standard-library APIs that require Python 3.9 or newer. In particular, avoid built-in generic annotations such as `list[str]`, PEP 604 unions such as `A | B`, structural pattern matching, and `tomllib` without a compatibility dependency.

Ruff and Pyright are both configured to evaluate the source as Python 3.8.

## Source map and dependency direction

- `src/fairyfishnet/__init__.py` exposes package metadata only. Import implementation objects from their owning modules.
- `cli.py` owns argument parsing, commands, signals, and long-running orchestration.
- `config.py` owns configuration files, prompts, validation, and normalized accessors.
- `engine.py` owns subprocess lifecycle and UCI request/response handling.
- `worker.py` owns server work units and per-worker engine execution.
- `variants.py` owns the generated default `variants.ini`, scoped definitions, leases, and cleanup.
- `downloads.py` owns CPU-specific engine selection, downloads, PyPI checks, and self-update.
- `cpuid.py` contains the isolated low-level CPUID implementation.
- `http_utils.py`, `logging_utils.py`, `constants.py`, and `errors.py` contain low-dependency shared helpers.
- `tests/` mirrors these boundaries; engine-backed tests live separately in `test_engine_integration.py`.

Prefer dependencies flowing from orchestration toward lower-level modules: `cli` → `worker` → `engine`, with `config` and `variants` as explicit collaborators. Avoid importing `cli` or `worker` from lower-level modules. When a configuration helper needs download functionality, use a narrow local import rather than creating a module import cycle.

The package root is not a service locator or public implementation API. Import functions and classes from their owning modules, such as `fairyfishnet.engine`, `fairyfishnet.variants`, or `fairyfishnet.worker`.

## Unit-test expectations

Every behavioral fix should add a focused unit test whenever the engine or network can be replaced with a small fake. Keep the `engine` marker only for tests that truly need a Fairy-Stockfish executable.

At minimum, preserve coverage for:

- configuration parsing, defaults, bounds, and error messages;
- HTTP invalid-JSON and update-version decisions;
- UCI option selection and variant-name transformations;
- worker request shape, job routing, and engine-crash recovery;
- scoped variant cache hits, downloads, atomic writes, active leases, and cleanup;
- the intentionally small package-root metadata API.

Tests must not depend on execution order or a shared `EngineDir`. Use temporary directories and monkeypatch the symbol in the module that owns the implementation.

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
