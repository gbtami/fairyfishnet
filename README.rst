fishnet: distributed Stockfish analysis for lichess.org
=======================================================

.. image:: https://travis-ci.org/niklasf/fishnet.svg?branch=master
    :target: https://travis-ci.org/niklasf/fishnet

.. image:: https://badge.fury.io/py/fishnet.svg
    :target: https://pypi.python.org/pypi/fishnet

Installing
----------

To install or upgrade to the latest version do:

::

    pip install --upgrade fishnet

Running fishnet
---------------

::

    python -m fishnet run

lichess.org custom Stockfish
----------------------------

fishnet is using
`lichess.org custom Stockfish <https://github.com/niklasf/Stockfish>`__
by `@ddugovic <https://github.com/ddugovic/Stockfish>`_.

You can build Stockfish yourself (for example with ``./build-stockfish.sh``)
and provide the path using ``python -m fishnet --engine-command``. Otherwise
a precompiled binary will be downloaded for you.

Overview
--------

.. image:: https://raw.githubusercontent.com/niklasf/fishnet/master/doc/sequence-diagram.png

See `protocol.md <https://github.com/niklasf/fishnet/blob/master/doc/protocol.md>`_ for details.

License
-------

fishnet is licensed under the MIT license. See LICENSE.txt for the full license
text.
