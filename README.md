fishnet
=======

Distributed Stockfish analysis for lichess.org

Usage
-----

Copy the default config and configure `EngineDir`,
`EngineCommand` and `Apikey` in `polyglot.ini`.

```
cp polyglot.ini.default polyglot.ini
```

Run the client:

```
python fishnet.py polyglot.ini
```

Protocol
--------

Client asks server:

```javascript
POST http://localhost:9000/fishnet/acquire

{
  "fishnet": {
    "version": "0.0.1",
    "apikey": "XXX"
  },
  "engine": {
    "name": "Stockfish 7 64",
    "author": "T. Romstad, M. Costalba, J. Kiiski, G. Linscott"
  }
}
```

```javascript
200 OK

{
  "work": {
    "type": "analysis",
    "id": "workid"
  },
  // or:
  // "work": {
  //   "type": "move",
  //   "id": "workid",
  //   "level": 5 // 1 to 8
  // },
  "game_id": "abcdefgh",
  "position": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "variant": "standard",
  "moves": "e2e4 c7c5 c2c4 b8c6 g1e2 g8f6 b1c3 c6b4 g2g3 b4d3"
}
```

Client runs Stockfish and sends the analysis to server:

```javascript
POST http://localhost:9000/fishnet/analysis/{work_id}

{
  "fishnet": {
    "version": "0.0.1",
    "apikey": "XXX"
  },
  "engine": {
    "name": "Stockfish 7 64",
    "author": "T. Romstad, M. Costalba, J. Kiiski, G. Linscott"
  },
  "analysis": [
    {  // first ply
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
    // ...
    {  // second last ply
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
    {  // last ply
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
POST http://localhost:9000/fishnet/move/{work_id}

{
  "fishnet": {
    "version": "0.0.1",
    "apikey": "XXX"
  },
  "engine": {
    "name": "Stockfish 7 64",
    "author": "T. Romstad, M. Costalba, J. Kiiski, G. Linscott"
  },
  "bestmove": "b7b8=Q"
}
```

```
202 Accepted
```
