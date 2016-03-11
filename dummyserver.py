#!/usr/bin/env python

import logging
import asyncio
import sys
import itertools
import json

import aiohttp.web

import chess
import chess.pgn


def jsonp(request, obj):
    json_str = json.dumps(obj, indent=2, sort_keys=True)

    return aiohttp.web.Response(
        text=json_str,
        content_type="application/json")


def fold_score(score, ply):
    if "cp" in score:
        return str(score["cp"] * (-1) ** ply)
    else:
        return "#%d" % (score["mate"] * (-1) ** ply)


class Api:
    def __init__(self, producer):
        self.producer = producer
        self.counter = itertools.count(1)
        self.games = {}

    def acquire(self, request):
        try:
            game, game_id = next(self.producer), next(self.counter)
            self.games[game_id] = game

            result = {
              "variant": "standard",
              "game_id": game_id,
              "position": game.board().fen(),
              "moves": []
            }

            node = game
            while not node.is_end():
                next_node = node.variation(0)
                result["moves"].append(next_node.move.uci())
                node = next_node

            return jsonp(request, result)
        except StopIteration:
            return aiohttp.web.HTTPNotFound(reason="that's all we got")

    async def post(self, request):
        game_id = int(request.match_info["id"])
        if game_id in self.games:
            data = json.loads((await request.content.read()).decode("utf-8"))
            analysis = data["analysis"]

            game = self.games[game_id]
            game.headers["Annotator"] = "fishnet %s using %s" % (data["version"], data["engine"]["name"])

            ply = 0
            node = game
            while not node.is_end():
                next_node = node.variation(0)
                node.comment = fold_score(analysis[ply]["score"], ply)
                ply += 1
                node = next_node

            node.comment = fold_score(analysis[ply]["score"], ply)

            print(game)
            return aiohttp.web.HTTPAccepted()
        else:
            return aiohttp.web.HTTPNotFound(reason="game id not found")


async def init(loop, producer):
    print("; ---")

    api = Api(producer)

    app = aiohttp.web.Application(loop=loop)
    app.router.add_route("POST", "/fishnet/acquire", api.acquire)
    app.router.add_route("POST", r"/fishnet/{id:\d+}", api.post)

    server = await loop.create_server(app.make_handler(), "127.0.0.1", 9000)
    print("; Listening on: http://localhost:9000/ ...")

    print("; ---")
    return server


def game_producer(pgn_file):
    while True:
        game = chess.pgn.read_game(pgn_file)
        if game is None:
            break

        yield game


if __name__ == "__main__":
    pgn_file = open(sys.argv[1], "r", encoding="utf-8-sig")

    logging.basicConfig(level=logging.DEBUG)
    loop = asyncio.get_event_loop()

    loop.run_until_complete(init(loop, game_producer(pgn_file)))
    loop.run_forever()
