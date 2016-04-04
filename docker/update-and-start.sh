#!/bin/sh

pip install --user --upgrade fishnet

python -m fishnet --no-conf $@
