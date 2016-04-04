#!/bin/sh

pip install --user --upgrade fishnet

exec python -m fishnet --no-conf $@
