# Coding-agent guide

This file is the repository-level operating guide for coding agents and contributors. Read it before making changes, then read the implementation and tests adjacent to the code you will edit. For worker/server protocol changes, also read `doc/protocol.md`.

The project is a production worker that executes untrusted network jobs through a long-lived Fairy-Stockfish subprocess. Prefer small, explicit changes over broad refactors, and preserve compatibility, failure recovery, and server authority unless the task deliberately changes them.

## Start here

The repository uses a `src/` layout, uv, pytest, Ruff, and Pyright. Python 3.8 is the compatibility floor even when development or CI runs on a newer interpreter.

Create the locked environment with the oldest supported Python:

```console
uv sync --locked --python 3.8
```

During development, run the narrowest relevant test first, for example:

```console
uv run pytest tests/test_variants.py
uv run pytest tests/test_worker.py -k crash
```

Before handing off a normal code change, run:

```console
uv run pytest -m "not engine"
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Also run `uv lock --check --python 3.8` and `uv build` when changing packaging, dependencies, supported Python versions, entry points, or release-related files. Run `uv run pytest -m engine` when the change affects engine startup, UCI behavior, downloaded engine selection, custom variant loading, or integration-test expectations. Engine tests may download or launch Fairy-Stockfish and can require network access on a clean checkout.

## Working rules

- Inspect the owning module, its tests, and relevant documentation before editing.
- Keep the patch scoped to the requested behavior. Avoid drive-by renames, formatting churn, and unrelated cleanup.
- Preserve existing CLI flags, configuration keys, protocol fields, abort reasons, and public import behavior unless the task explicitly changes an interface.
- Add or update focused tests for behavioral changes. Prefer small fakes and monkeypatching over real network calls or engine processes.
- Patch symbols where they are looked up by the implementation, not where they were originally defined.
- Use temporary directories for filesystem tests. Do not depend on a real `fishnet.ini`, API key, engine cache, installed Stockfish binary, or shared `EngineDir`.
- Do not put credentials, API keys, complete server responses, or sensitive URLs into logs, fixtures, snapshots, or error messages. Preserve the API-key censoring behavior.
- Do not bump the package version, create tags, publish, or run `scripts/release.py` unless explicitly requested.
- Do not edit `uv.lock` unless project or development dependencies change. When dependencies do change, run `uv lock` and commit the resulting lockfile update with the metadata change.
- Do not add runtime dependencies for functionality that can reasonably use the standard library or an existing dependency.
- Keep user-facing errors actionable and include the failed operation or resource, but avoid leaking secrets.

## Python compatibility and style

Python 3.8 is the oldest supported interpreter. Ruff and Pyright are both configured to evaluate the source as Python 3.8.

Do not introduce syntax or standard-library APIs that require Python 3.9 or newer. In particular, avoid:

- built-in generic annotations such as `list[str]`;
- PEP 604 unions such as `A | B`;
- structural pattern matching;
- `tomllib` without a compatibility dependency.

Follow the existing style rather than modernizing nearby code incidentally. Keep imports compatible with the `src/` layout and import implementation objects from their owning modules.

## Source map and dependency direction

- `src/fairyfishnet/__init__.py` exposes package metadata only.
- `cli.py` owns argument parsing, commands, signals, process respawning, and long-running orchestration.
- `config.py` owns configuration files, prompts, validation, and normalized accessors.
- `engine.py` owns subprocess lifecycle and ordered UCI request/response handling.
- `worker.py` owns server work units, request routing, progress reporting, and per-worker engine execution.
- `variants.py` owns server-provided `variants.ini` downloads, content-addressed cache entries, leases, cleanup, and serialized pyffish variant loading.
- `downloads.py` owns CPU-specific engine selection, downloads, PyPI checks, and self-update.
- `cpuid.py` contains the isolated low-level CPUID implementation.
- `dependencies.py` centralizes third-party imports used by runtime modules.
- `http_utils.py`, `logging_utils.py`, `constants.py`, and `errors.py` contain low-dependency shared helpers.
- `tests/` mirrors these boundaries; engine-backed tests live separately in `test_engine_integration.py`.

Prefer dependencies flowing from orchestration toward lower-level modules: `cli` → `worker` → `engine`, with `config` and `variants` as explicit collaborators. Avoid importing `cli` or `worker` from lower-level modules. When a configuration helper needs download functionality, use a narrow local import rather than creating a module import cycle.

The package root is not a service locator or public implementation API. Import functions and classes from modules such as `fairyfishnet.engine`, `fairyfishnet.variants`, or `fairyfishnet.worker`. Preserve the intentionally small package-root API tested by `tests/test_public_api.py`.

## Protocol and server authority

The pychess server is the sole authority for site and user-defined variant rules. Every move or analysis job must include `variantsSha256`; fairyfishnet downloads `/fishnet/variants/<key>` on a cache miss and verifies both the response hash and the actual content hash.

Never add pychess site rules to the fairyfishnet package. Never silently substitute bundled, newer, different-scope, or stale variant rules. If the exact payload cannot be obtained or verified, abort that work unit with the structured variants-unavailable reason.

Protocol changes must be coordinated with the server. Preserve JSON field names, accepted HTTP statuses, retry/backoff semantics, and progress/abort behavior unless the task explicitly changes the protocol. Use `response_json()` for contextual invalid-JSON errors and bounded response snippets rather than dumping arbitrary response bodies.

All network operations need explicit timeouts and status validation. Downloads and cache writes should use temporary files and atomic replacement where practical. A transient network or server failure must not corrupt an existing usable file.

Startup engine validation uses a temporary synthetic `[fishnet-smoke:chess]` definition. It checks custom INI loading without coupling a fairyfishnet release to the current pychess variant catalog. Legacy `VariantPath` entries in `fishnet.ini` are ignored because the worker owns this option per work unit.

## Engine process and threading invariants

Each `Worker` owns its Fairy-Stockfish subprocess. Never share a subprocess between worker threads.

All UCI commands must preserve request/response ordering. A command that changes options or variants must complete an `isready` round trip before analysis or move generation continues. Do not add concurrent reads or writes to the same engine pipes.

EOF, broken-pipe, and timeout paths must abort the current job with a structured reason and restart the engine rather than terminating the worker thread. Cleanup and shutdown paths should remain safe when the engine is already dead or only partially initialized.

CPU capability detection launches `python -m fairyfishnet cpuid` as a subprocess. The `cpuid` command must not perform configuration loading, network access, engine startup, dependency chatter, or ordinary logging before writing its machine-readable rows.

When changing shared state, reason about both multiple worker threads in one process and multiple fairyfishnet processes sharing an `EngineDir`. Do not rely on the GIL for filesystem or cross-process correctness.

## Dynamic variant files

Server-provided variant definitions are content-addressed as `variants-<sha256>.ini` in `EngineDir`.

Preserve these rules:

1. Validate the expected lowercase SHA-256 before selecting a cached file.
2. Verify the content hash before publishing a downloaded file.
3. Write new files atomically through a temporary file and `os.replace`.
4. Pass the selected filename directly to the worker that requested it; do not rely on a mutable process-global current filename.
5. Serialize pyffish `VariantPath` changes because pyffish stores that option globally in-process.
6. Mark an entry active for the complete engine job and publish the process lease before it can be cleaned.
7. Cleanup may delete only exact scoped-cache filenames that are old, outside the retained newest set, and absent from all live leases.
8. Never delete unrelated `.ini` files; only exact `variants-<sha256>.ini` cache entries are managed.

Any cache-policy change needs tests for hash rejection, cache reuse, active entries, recent entries, retained newest entries, cross-process leases, and unrelated files as applicable.

## Engine downloads, aliases, and NNUE

Engine and network downloads are untrusted I/O. Keep explicit HTTP timeouts, status validation, bounded error details, and temporary/atomic replacement where practical. Never replace a working engine or network file with a partial download.

Variant aliases used for NNUE selection must remain compatible with the names sent by pychess and accepted by Fairy-Stockfish. Test canonical variants and at least one alias when changing mappings. Keep startup capability validation independent from the broader set of variants that may have NNUE networks.

## CLI, configuration, updates, and packaging

The installed command is `fairyfishnet`; `python -m fairyfishnet` must remain equivalent because subprocess detection, systemd units, and respawning rely on it.

Configuration changes should preserve existing files where possible. Validate values at the boundary, keep normalized access through `conf_get()`, and retain clear errors for invalid endpoints, keys, core/thread counts, memory, and engine commands.

Self-update intentionally retains the established pip-based behavior:

```console
<current-interpreter> -m pip install [--user] --upgrade <release-url>
```

Development, locking, testing, and publishing use uv, but changing the worker updater is a runtime behavior change. After installation, the worker respawns with the same interpreter and `-m fairyfishnet`. When installing as an isolated uv tool, include pip with `uv tool install --with pip fairyfishnet`.

The Dockerfile uses uv's pip-compatible interface to install the latest released `fairyfishnet` package from PyPI into the container's system Python. It must not copy and install the repository checkout unless that deployment policy is changed deliberately.

Package metadata has a single version source in `pyproject.toml`. Runtime code obtains it from installed distribution metadata; do not restore build-time imports of the worker module.

## Test expectations

Every behavioral fix should add a focused unit test whenever the engine or network can be replaced with a small fake. Keep the `engine` marker only for tests that truly need a Fairy-Stockfish executable.

At minimum, preserve coverage for:

- configuration parsing, defaults, bounds, compatibility, and error messages;
- HTTP invalid-JSON handling, status context, downloads, and update-version decisions;
- UCI option selection, request ordering, timeout handling, and variant-name transformations;
- worker request shape, job routing, progress/abort behavior, and engine-crash recovery;
- scoped variant cache hits, downloads, atomic writes, active leases, and cleanup;
- the intentionally small package-root metadata API.

Tests must not depend on execution order, wall-clock timing without control, external service availability, or a shared `EngineDir`. Use deterministic fakes for requests, subprocesses, clocks, sleeps, and random update decisions where needed.

## Validation by change type

- Documentation-only: inspect the rendered Markdown and check links/commands against the repository.
- Pure helper or validation change: run the owning test file, then the fast suite and quality checks.
- Worker, HTTP, or protocol change: run the owning tests plus the full fast suite; update `doc/protocol.md` when the wire contract changes.
- Engine/UCI/custom-variant change: run focused unit tests, the fast suite, and `pytest -m engine` when the environment permits.
- Dependency, packaging, entry-point, or Python-support change: run the complete validation checklist, lock check, and build.
- Concurrency or cache change: test the focused behavior and explicitly review thread, process, shutdown, stale-lock, retry, and partial-write cases.

For concurrency or cache work, explicitly consider:

- multiple worker threads in one process;
- multiple fairyfishnet processes sharing an `EngineDir`;
- process termination while a job, lease update, cleanup, or atomic write is in progress;
- a server retry delivering the same variant hash;
- a job missing its required hash, an unavailable exact server payload, or a server hash mismatch.

## Handoff

Summarize what changed, which tests and checks ran, and any checks that could not run because they require network access or a Fairy-Stockfish executable. Call out protocol, compatibility, configuration, or deployment implications explicitly. Do not claim a check passed unless it was actually executed.
