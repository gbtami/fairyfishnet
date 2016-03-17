#!/bin/sh

git submodule update --init
cd Stockfish/src
make build ARCH=x86-64-modern
cd ../..
