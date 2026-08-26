Protocol
========

![Fishnet sequence diagram](https://raw.githubusercontent.com/niklasf/fishnet/master/doc/sequence-diagram.png)

Client asks server:

```javascript
POST http://lichess.org/fishnet/acquire

{
  "fishnet": {
    "version": "1.15.7",
    "python": "2.7.11+",
    "apikey": "XXX"
  },
  "engine": {
    "name": "Stockfish 7 64",
    "options": {
      "hash": "256",
      "threads": "4"
    }
  }
}
```

```javascript
200 OK

{
  "work": {
    "type": "analysis",
    "id": "work_id"
  },
  // or:
  // "work": {
  //   "type": "move",
  //   "id": "work_id",
  //   "level": 5 // 1 to 8
  // },
  "game_id": "abcdefgh", // optional
  "position": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", // start position (X-FEN)
  "variant": "standard",
  // For a site or user-defined variant only:
  // "variantsSha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  // "variantsScope": "customvariant",
  "moves": "e2e4 c7c5 c2c4 b8c6 g1e2 g8f6 b1c3 c6b4 g2g3 b4d3", // moves of the game (UCI)
  "nodes": 3500000, // optional limit
  "skipPositions": [1, 4, 5] // 0 is the first position
}
```


Variant rules
-------------

Fairy-Stockfish built-in variants need no custom-rule payload. The client captures that built-in catalog from `pyffish.variants()` before loading any INI.

Work units for site-defined or user-defined variants include the SHA-256 of the exact, minimal `variants.ini` definition chain required for that job. On a cache miss the client requests it from the same server:

```
GET /fishnet/variants/{apikey}?sha256={variantsSha256}&variant={variantsScope}
```

A successful response contains `variantsIni`, the matching `variantsSha256`, and optionally `variantsScope`. The payload contains only the requested custom section and any custom base sections it inherits from; unrelated site and user-defined variants are omitted. The client verifies the declared hash and the UTF-8 content hash before atomically caching the file as `variants-<sha256>.ini`. The client must abort a non-built-in work unit if the hash is missing, the exact payload is unavailable (`409 Conflict`), or any hash differs. It must never substitute a bundled or newer rules file.

Fairy-Stockfish keys loaded definitions by their INI section name and cannot replace one in a running engine process. The client fingerprints every section loaded into each engine. If an exact payload reuses a loaded section name with different rules, the client restarts that engine before loading the requested file. This preserves the content-addressed contract even when an older server has allowed an internal variant name to be reused.

The server is the source of truth for both site variants and user-defined variants. Adding or editing INI-only site rules therefore requires a pychess deployment, not a fairyfishnet package release.

Client runs Stockfish and sends the analysis to server.
The client can optionally report progress to the server, by sending null for
the pending moves in `analysis`.

```javascript
POST http://lichess.org/fishnet/analysis/{work_id}

{
  "fishnet": {
    "version": "0.0.1",
    "python": "2.7.11+",
    "apikey": "XXX"
  },
  "engine": {
    "name": "Stockfish 7 64",
    "author": "T. Romstad, M. Costalba, J. Kiiski, G. Linscott"
    "options": {
      "hash": "256",
      "threads": "4"
    }
  },
  "analysis": [
    { // first ply
      "pv": "e2e4 e7e5 g1f3 g8f6",
      "seldepth": 24,
      "tbhits": 0,
      "depth": 18,
      "score": {
        "cp": 24
      },
      "time": 1004,
      "nodes": 1686023,
      "nps": 1670251
    },
    { // second ply (1 was in skipPositions)
      "skipped": true
    },
    // ...
    { // second last ply
      "pv": "b4d3",
      "seldepth": 2,
      "tbhits": 0,
      "depth": 127,
      "score": {
        "mate": 1
      },
      "time": 3,
      "nodes": 3691,
      "nps": 1230333
    },
    { // last ply
      "depth": 0,
      "score": {
        "mate": 0
      }
    }
  ]
}
```

Or the move:

```javascript
POST http://lichess.org/fishnet/move/{work_id}

{
  "fishnet": {
    "version": "0.0.1",
    "python": "2.7.11+",
    "apikey": "XXX"
  },
  "engine": {
    "name": "Stockfish 7 64",
    "author": "T. Romstad, M. Costalba, J. Kiiski, G. Linscott"
    "options": {
      "hash": "256",
      "threads": "4"
    }
  },
  "bestmove": "b7b8q"
}
```

Accepted:

```
204 No content
```

Accepted, with next job:

```
202 Accepted

[...]
```

Aborting jobs
-------------

The client should send a request like the following, when shutting down instead
of completing an analysis. The server can then immediately give the job to
another client.

```
POST http://lichess.org/fishnet/abort/{work_id}

{
  "fishnet": {
    "version": "0.0.1",
    "python": "2.7.11+",
    "apikey": "XXX"
  },
  "engine": {
    "name": "Stockfish 7 64",
    "author": "T. Romstad, M. Costalba, J. Kiiski, G. Linscott"
    "options": {
      "hash": "256",
      "threads": "4"
    }
  }
}
```

Response:

```
204 No Content
```
