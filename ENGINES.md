# Engine routing and optional capabilities

fairyfishnet normally uses the pychess-variants build of Fairy-Stockfish for every
job. A worker may additionally configure dedicated engines for variants that the
main Fairy-Stockfish build does not support. Dedicated engines are optional worker
capabilities, not global requirements.

## Capability model

A worker advertises an optional variant only after the corresponding engine passes
startup validation. For example, Alice Chess is advertised as:

```json
"capabilities": {
  "variants": ["alice"]
}
```

If the Alice engine is not configured, cannot execute on the current CPU, fails UCI
startup, rejects its configured options, or cannot complete the validation search,
the worker continues normally and advertises no `alice` capability. Older
fairyfishnet clients that do not send `capabilities` are therefore compatible with
ordinary Fairy-Stockfish jobs but are not eligible for optional-engine jobs.

The server is responsible for matching optional-capability work to a compatible
worker. A worker may encounter incompatible work in the shared queue; the server
defers that work and can return another job instead.

The capability declaration describes only work that this fairyfishnet worker can
perform. It does not imply that a web client, browser/WASM engine, PGN parser, or
other PyChess execution path supports the same variant.

## Engine lifecycle

Here `Worker` means the internal `fairyfishnet.worker.Worker` thread, not the whole
fairyfishnet client that the PyChess server commonly refers to as a fishnet worker.
A single fairyfishnet client can create multiple internal `Worker` threads according
to its configured `Cores` and `Threads`, and therefore can run multiple engine
subprocesses concurrently. The one-subprocess limit applies independently to each
internal `Worker`.

Each internal `Worker` owns at most one engine subprocess at a time. The engine kind
is chosen from the acquired job:

- ordinary variants -> Fairy-Stockfish;
- `alice` -> Alice-Stockfish, when that optional engine is available.

When the next job requires another engine kind, the current subprocess is stopped
and the required engine is started. This avoids keeping a second engine and hash
table resident while it is idle.

Fairy-Stockfish receives the existing dynamic variant setup, including
`UCI_Variant` and custom `variants.ini` handling. A dedicated single-variant engine
must not automatically receive those Fairy-Stockfish-specific commands. In
particular, Alice-Stockfish 1.0 advertises a single-value
`UCI_Variant` (`default alice var alice`) and already starts in Alice mode, so
fairyfishnet verifies that advertisement but does not send
`setoption name UCI_Variant value alice`.

## Alice-Stockfish

Configure Alice support in `fishnet.ini`:

```ini
[Fishnet]
AliceStockfishCommand = /path/to/alice-stockfish

[AliceStockfish]
# Optional UCI settings for the dedicated engine.
# EvalFile = /path/to/Alice_v1.nnue
```

Startup validation checks that the executable:

1. starts and completes `uci`;
2. advertises `alice` through `UCI_Variant`;
3. accepts the configured `[AliceStockfish]` options;
4. becomes ready; and
5. completes a shallow search.

Only then is `alice` included in the worker capability payload. Failure disables
Alice support for that worker without disabling Fairy-Stockfish work.

The `[AliceStockfish]` section is deliberately separate from `[Stockfish]`; options
valid for one engine are not assumed to exist on the other. For example,
Alice-Stockfish 1.0 does not advertise `UCI_AnalyseMode`.

## Adding another dedicated engine

When adding optional support for another variant, keep the same boundaries:

1. make the executable/configuration optional;
2. validate the engine before advertising its capability;
3. advertise a stable variant key in `fishnet.capabilities.variants`;
4. route only matching jobs to the dedicated engine;
5. keep engine-specific UCI setup separate from Fairy-Stockfish setup;
6. preserve one-engine-per-worker lifecycle unless there is a measured reason not to;
7. keep absence or validation failure non-fatal for ordinary work; and
8. add server-side scheduling support before advertising the new capability in
   production.

The wire format is documented in [doc/protocol.md](doc/protocol.md).
