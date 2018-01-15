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
  "position": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "variant": "standard",
  "moves": "e2e4 c7c5 c2c4 b8c6 g1e2 g8f6 b1c3 c6b4 g2g3 b4d3",
  "nodes": 3500000, // optional limit
  "skipPositions": [1, 5] // 0 is the first position
}
```

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
