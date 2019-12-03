fairyfishnet: distributed Fairy-Stockfish analysis for pychess-variants
========================================================================

.. image:: https://badge.fury.io/py/fishnet.svg
    :target: https://pypi.python.org/pypi/fairyfishnet

.. image:: https://travis-ci.org/niklasf/fishnet.svg?branch=master
    :target: https://travis-ci.org/niklasf/fishnet

.. image:: https://coveralls.io/repos/github/niklasf/fishnet/badge.svg?branch=master
    :target: https://coveralls.io/github/niklasf/fishnet?branch=master

Installation
------------

1. Request your personal fairyfishnet key: https://pychess-variants.herokuapp.com
2. Install the fairyfishnet client.

   **Via pip**

   To install or upgrade to the latest version do:

   ::

       pip3 install --upgrade --user fairyfishnet

   Example usage:

   ::

       python3 -m fairyfishnet --auto-update

   Optional: Generate a systemd service file:

   ::

       python3 -m fairyfishnet systemd

   **Via Docker**

   There is a `Docker container <https://hub.docker.com/r/mklemenz/fishnet/>`_
   courtesy of `@mklemenz <https://github.com/mklemenz>`_. For example you can
   simply do:

   ::

       docker run mklemenz/fishnet --key MY_APIKEY --auto-update

pychess-variants custom Fairy-Stockfish
---------------------------------------

fairyfishnet is using
`Fairy-Stockfish <https://github.com/ianfab/Fairy-Stockfish>`__
by `@ianfab <https://github.com/ianfab/Fairy-Stockfish>`_.

You can build Fairy-Stockfish yourself (for example with ``./build-stockfish.sh``)
and provide the path using ``python -m fairyfishnet --stockfish-command``. Otherwise
a precompiled binary will be downloaded for you.

Overview
--------

.. image:: https://raw.githubusercontent.com/gbtami/fairyfishnet/master/doc/sequence-diagram.png

See `protocol.md <https://github.com/gbtami/fairyfishnet/blob/master/doc/protocol.md>`_ for details.

License
-------

fairyfishnet is licensed under the GPLv3+ license. See LICENSE.txt for the full
license text.
