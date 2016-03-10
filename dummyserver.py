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


class Api:
    def __init__(self, producer):
        self.producer = producer
        self.counter = itertools.count(1)

    def get(self, request):
        try:
            game, game_id = next(self.producer), next(self.counter)

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


async def init(loop, producer):
    print("---")

    api = Api(producer)

    app = aiohttp.web.Application(loop=loop)
    app.router.add_route("GET", "/", api.get)

    server = await loop.create_server(app.make_handler(), "127.0.0.1", 9000)
    print("Listening on: http://localhost:9000/ ...")

    print("---")
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
