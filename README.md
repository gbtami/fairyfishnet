# fairyfishnet

[![PyPI version](https://badge.fury.io/py/fairyfishnet.svg)](https://pypi.org/project/fairyfishnet/)

Distributed [Fairy-Stockfish](https://github.com/ianfab/Fairy-Stockfish) analysis for [pychess.org](https://www.pychess.org/).

fairyfishnet requires Python 3.10 or newer.

## Installation

1. Request a personal fairyfishnet key on the [pychess Discord server](https://discord.gg/aPs8RKr).
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
3. Install the worker as an isolated command-line tool:

   ```console
   uv tool install --with pip fairyfishnet
   ```

4. Start the worker and follow the configuration prompts:

   ```console
   fairyfishnet --auto-update
   ```

To upgrade manually:

```console
uv tool upgrade fairyfishnet
```

The extra `pip` package keeps the worker's existing `--auto-update` behavior available inside the isolated uv tool environment.

### systemd

Generate a service file after configuring the worker:

```console
fairyfishnet systemd
```

The command prints a service definition that can be reviewed and installed under `/etc/systemd/system/`.

### Docker

Build the image. The Dockerfile installs the latest released `fairyfishnet` package from PyPI; it does not install the current checkout:

```console
docker build -t fairyfishnet .
docker run --rm fairyfishnet --key MY_API_KEY --auto-update
```

## Resource allocation

The `[Fishnet]` section of `fishnet.ini` controls how the selected logical CPU cores and engine hash memory are divided among Fairy-Stockfish processes:

```ini
[Fishnet]
Cores = 4
Threads = 2
Memory = 256
```

- `Cores` is the total number of engine threads. `auto` uses all but one logical CPU, while `all` uses every logical CPU reported by Python.
- `Threads` is a hint for the number of threads per engine process. Its default is 3, or fewer when fewer cores are selected.
- `Memory` is the total Fairy-Stockfish transposition-table (UCI `Hash`) budget in MB across all engine processes. It is not a limit for total process RAM and is not calculated from available system memory.

The number of engine processes is `max(1, Cores // Threads)`. All selected cores are then distributed as evenly as possible among those processes, so the actual thread count can be slightly higher than the `Threads` hint. `Memory = auto` assigns 256 MB of hash per process. A manual value is divided among the processes and must provide each one between 16 and 512 MB of hash.

For example, an 8-thread machine with `Cores = auto` selects 7 cores:

| `Threads` | Engine processes | Actual engine threads | `Memory = auto` |
| --- | ---: | --- | ---: |
| `auto` or `3` | 2 | 4 + 3 | 512 MB total |
| `2` | 3 | 3 + 2 + 2 | 768 MB total |
| `1` | 7 | 1 each | 1792 MB total |

The example configuration above starts two 2-thread engines and gives each engine 128 MB of hash. Operating-system memory usage will be higher because each process also uses memory for the engine, NNUE networks, thread state, and other data. More pages may become resident while an engine is searching.

Fewer threads per process allow more concurrent jobs and generally favor total queue throughput. More threads per process reduce concurrency but can lower the latency of an individual job. fairyfishnet uses this static allocation for its lifetime; it does not dynamically move cores between busy and idle engines.

The equivalent command-line options are `--cores`, `--threads-per-process`, and `--memory`.

## Fairy-Stockfish

fairyfishnet uses the pychess-variants build of [Fairy-Stockfish](https://github.com/ianfab/Fairy-Stockfish).

A suitable precompiled engine is downloaded automatically. To use a locally built engine, run `./build-stockfish.sh` and pass its path with `--stockfish-command`.

Engine lifecycle, UCI, dynamic variant, and cache invariants are documented in [ENGINES.md](ENGINES.md).

## Development

The repository uses a `src/` package layout and keeps tests under `tests/`.

Create the locked development environment using the oldest supported Python:

```console
uv sync --locked --python 3.10
```

Run the fast tests and quality checks:

```console
uv run pytest -m "not engine"
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv lock --check --python 3.10
uv build
```

The engine integration tests download or launch Fairy-Stockfish:

```console
uv run pytest -m engine
```

Apply formatting with:

```console
uv run ruff format .
```

Whenever project or development dependencies change, refresh and commit the lockfile:

```console
uv lock
```

### Repository layout

```text
src/fairyfishnet/__init__.py   package metadata only
src/fairyfishnet/cli.py        argument parsing, commands, signals, and worker orchestration
src/fairyfishnet/config.py     configuration loading and validation
src/fairyfishnet/engine.py     subprocess management and the UCI protocol
src/fairyfishnet/worker.py     job acquisition, move generation, and analysis
src/fairyfishnet/variants.py   server variants.ini download and scoped cache lifecycle
src/fairyfishnet/downloads.py  engine downloads and self-update handling
src/fairyfishnet/cpuid.py      low-level CPU capability probing
src/fairyfishnet/http_utils.py HTTP and release-version helpers
tests/                         focused unit tests and engine integration tests
scripts/                       release and maintenance helpers
doc/                           fishnet protocol documentation
```

The fast suite is intentionally split by subsystem, so a regression normally points to the module that owns the behavior. Tests marked `engine` download or launch Fairy-Stockfish and are therefore slower and require network access on a clean checkout.

## Protocol

See [doc/protocol.md](doc/protocol.md) for the worker/server protocol.

![Sequence diagram](doc/sequence-diagram.png)

## Releasing

Set `UV_PUBLISH_TOKEN`, then run:

```console
uv run python scripts/release.py
```

The release helper runs tests, Ruff, Pyright, builds distributions, verifies a clean Git tree, creates the version tag, pushes it, and publishes with uv.

## License

fairyfishnet is licensed under GPL-3.0-or-later. See [LICENSE.txt](LICENSE.txt).
