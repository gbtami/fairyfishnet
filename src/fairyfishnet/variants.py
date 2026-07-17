# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Default and scoped variants.ini generation and cache management."""

import collections
import contextlib
import errno
import hashlib
import logging
import os
import random
import re
import textwrap
import threading
import time

from .config import get_endpoint, get_engine_dir, get_key, load_conf
from .constants import HTTP_TIMEOUT
from .dependencies import requests, sf
from .engine import isready, setoption
from .errors import ConfigError, JsonResponseError
from .http_utils import response_json

VARIANTS_CACHE_MAX_FILES = 512
VARIANTS_CACHE_MIN_AGE = 7 * 24 * 60 * 60
VARIANTS_CACHE_CLEANUP_INTERVAL = 24 * 60 * 60
VARIANTS_CACHE_LEASE_TTL = 15 * 60
VARIANTS_CACHE_LOCK_STALE = 10
VARIANTS_CACHE_FILE_RE = re.compile(r"^variants-([0-9a-f]{64})\.ini$")
VARIANTS_CACHE_LEASE_RE = re.compile(r"^\.fairyfishnet-variants-[0-9]+-[0-9a-f]+\.lease$")
VARIANTS_CACHE_THREAD_LOCK = threading.RLock()
VARIANTS_LEASE_LOCK = threading.RLock()
PYFFISH_VARIANT_LOCK = threading.RLock()
ACTIVE_VARIANTS = collections.Counter()
VARIANTS_LEASE_TOKEN = "%d-%x" % (os.getpid(), int(time.time() * 1000000))
VariantsIni = collections.namedtuple("VariantsIni", "sha256 filename path")


def _valid_variants_sha256(value):
    return isinstance(value, str) and re.match(r"^[0-9a-f]{64}$", value) is not None


def variants_ini_filename(sha256=None):
    if _valid_variants_sha256(sha256):
        return "variants-%s.ini" % sha256
    return "variants.ini"


def variants_ini_path(conf, sha256=None):
    return os.path.join(get_engine_dir(conf), variants_ini_filename(sha256))


def default_variants_ini(conf):
    return VariantsIni(None, "variants.ini", variants_ini_path(conf))


def _variants_ini_entry(conf, sha256):
    filename = variants_ini_filename(sha256)
    return VariantsIni(sha256, filename, os.path.join(get_engine_dir(conf), filename))


def _atomic_write_text(path, text):
    temporary = "%s.tmp-%d-%d-%x" % (
        path,
        os.getpid(),
        id(threading.current_thread()),
        random.getrandbits(32),
    )
    try:
        with open(temporary, "w") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise


def _touch(path):
    try:
        os.utime(path, None)
    except OSError as err:
        if err.errno != errno.ENOENT:
            raise


@contextlib.contextmanager
def _variants_cache_lock(conf, timeout=VARIANTS_CACHE_LOCK_STALE + 1.0):
    lock_path = os.path.join(get_engine_dir(conf), ".fairyfishnet-variants-cache.lock")
    lock_token = "%s-%x" % (VARIANTS_LEASE_TOKEN, random.getrandbits(64))
    deadline = time.time() + timeout
    acquired = False

    with VARIANTS_CACHE_THREAD_LOCK:
        while not acquired:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError as err:
                if err.errno != errno.EEXIST:
                    raise

                try:
                    stale = time.time() - os.path.getmtime(lock_path) > VARIANTS_CACHE_LOCK_STALE
                except OSError:
                    stale = False

                if stale:
                    try:
                        os.remove(lock_path)
                    except OSError:
                        pass
                    continue

                if time.time() >= deadline:
                    raise IOError("Timed out waiting for variants.ini cache lock")
                time.sleep(0.05)
            else:
                with os.fdopen(descriptor, "w") as lock_file:
                    lock_file.write(lock_token)
                acquired = True

        try:
            yield
        finally:
            try:
                with open(lock_path) as lock_file:
                    owner = lock_file.read()
            except OSError:
                owner = None
            if owner == lock_token:
                try:
                    os.remove(lock_path)
                except OSError as err:
                    if err.errno != errno.ENOENT:
                        logging.warning("Could not remove variants.ini cache lock: %s", err)


def write_variants_ini(conf, ini_text, sha256=None, scoped=False):
    payload_sha256 = sha256 or hashlib.sha256(ini_text.encode("utf-8")).hexdigest()
    entry = _variants_ini_entry(conf, payload_sha256) if scoped else default_variants_ini(conf)

    with _variants_cache_lock(conf):
        if not scoped or not os.path.exists(entry.path):
            _atomic_write_text(entry.path, ini_text)
        else:
            _touch(entry.path)

    if not scoped:
        entry = VariantsIni(payload_sha256, entry.filename, entry.path)
    return entry


def _load_cached_variants_ini(conf, expected_sha256):
    if not _valid_variants_sha256(expected_sha256):
        return None

    entry = _variants_ini_entry(conf, expected_sha256)
    with _variants_cache_lock(conf):
        if not os.path.exists(entry.path):
            return None
        _touch(entry.path)
    return entry


def sync_variants_ini(conf, expected_sha256=None, required=False, variant=None):
    """Return the immutable variants.ini cache entry required by a work unit.

    Older deployed pychess servers do not provide /fishnet/variants/<key>. The
    worker therefore uses its bundled variants.ini unless a newer server sends
    a variantsSha256 value with a job. Scoped payloads are content-addressed so
    worker threads and separate fairyfishnet processes can safely share them.
    """

    if not expected_sha256:
        return default_variants_ini(conf)

    try:
        cached = _load_cached_variants_ini(conf, expected_sha256)
    except IOError as err:
        logging.warning("Could not read variants.ini cache: %s", err)
        cached = None
    if cached:
        return cached

    key = get_key(conf)
    if not key:
        return None

    params = {"sha256": expected_sha256}
    if variant:
        params["variant"] = variant

    try:
        response = requests.get(get_endpoint(conf, "variants/%s" % key), params=params, timeout=HTTP_TIMEOUT)
        if response.status_code == 404:
            raise ConfigError("Invalid or inactive fishnet key")
        response.raise_for_status()
        payload = response_json(response, "fishnet variants.ini")
        ini_text = payload["variantsIni"]
        sha256 = payload.get("variantsSha256") or hashlib.sha256(ini_text.encode("utf-8")).hexdigest()
        if sha256 != expected_sha256:
            logging.warning(
                "Fetched variants.ini hash %s, but server job expected %s",
                sha256,
                expected_sha256,
            )
        entry = write_variants_ini(conf, ini_text, sha256, scoped=True)
        logging.info("Updated scoped variants.ini from server (%s)", sha256[:12])
        return entry
    except JsonResponseError as err:
        if required:
            raise
        logging.warning(
            "Could not fetch variants.ini from server (%s); using local fallback if available.",
            err,
        )
        return None
    except Exception:
        if required:
            raise
        logging.warning(
            "Could not fetch variants.ini from server; using local fallback if available.",
            exc_info=True,
        )
        return None


def _variants_lease_path(conf):
    return os.path.join(get_engine_dir(conf), ".fairyfishnet-variants-%s.lease" % VARIANTS_LEASE_TOKEN)


def _active_variants_for_engine_dir(engine_dir):
    return sorted(
        sha256
        for (active_engine_dir, sha256), count in ACTIVE_VARIANTS.items()
        if active_engine_dir == engine_dir and count > 0
    )


def _write_variants_lease(conf):
    engine_dir = get_engine_dir(conf)
    path = _variants_lease_path(conf)
    active = _active_variants_for_engine_dir(engine_dir)
    if active:
        _atomic_write_text(path, "".join("%s\n" % sha256 for sha256 in active))
    else:
        try:
            os.remove(path)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise


def _try_write_variants_lease(conf):
    try:
        _write_variants_lease(conf)
    except OSError as err:
        logging.warning("Could not update variants.ini cache lease: %s", err)


def refresh_variants_ini_lease(conf):
    with VARIANTS_LEASE_LOCK:
        _try_write_variants_lease(conf)


def clear_variants_ini_lease(conf):
    engine_dir = get_engine_dir(conf)
    with VARIANTS_LEASE_LOCK:
        for key in list(ACTIVE_VARIANTS):
            if key[0] == engine_dir:
                del ACTIVE_VARIANTS[key]
        _try_write_variants_lease(conf)


@contextlib.contextmanager
def active_variants_ini(conf, entry):
    if entry is None or not _valid_variants_sha256(entry.sha256):
        yield
        return

    key = (get_engine_dir(conf), entry.sha256)
    with VARIANTS_LEASE_LOCK:
        ACTIVE_VARIANTS[key] += 1
        _try_write_variants_lease(conf)
    try:
        yield
    finally:
        with VARIANTS_LEASE_LOCK:
            ACTIVE_VARIANTS[key] -= 1
            if ACTIVE_VARIANTS[key] <= 0:
                del ACTIVE_VARIANTS[key]
            _try_write_variants_lease(conf)


def _leased_variants_ini_hashes(engine_dir, now):
    protected = set()
    try:
        names = os.listdir(engine_dir)
    except OSError:
        return protected

    for name in names:
        if VARIANTS_CACHE_LEASE_RE.match(name) is None:
            continue
        path = os.path.join(engine_dir, name)
        try:
            modified = os.path.getmtime(path)
        except OSError:
            continue
        if now - modified > VARIANTS_CACHE_LEASE_TTL:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            with open(path) as lease:
                protected.update(line.strip() for line in lease if _valid_variants_sha256(line.strip()))
        except OSError:
            continue
    return protected


def cleanup_variants_ini_cache(conf, now=None, max_files=VARIANTS_CACHE_MAX_FILES, min_age=VARIANTS_CACHE_MIN_AGE):
    """Delete old, unused content-addressed variants.ini cache entries."""

    now = time.time() if now is None else now
    engine_dir = get_engine_dir(conf)
    deleted = 0

    try:
        with _variants_cache_lock(conf):
            protected = _leased_variants_ini_hashes(engine_dir, now)
            with VARIANTS_LEASE_LOCK:
                protected.update(_active_variants_for_engine_dir(engine_dir))
            entries = []
            for name in os.listdir(engine_dir):
                match = VARIANTS_CACHE_FILE_RE.match(name)
                if match is None:
                    continue
                path = os.path.join(engine_dir, name)
                try:
                    modified = os.path.getmtime(path)
                except OSError:
                    continue
                entries.append((modified, match.group(1), path))

            entries.sort(reverse=True)
            for index, (modified, sha256, path) in enumerate(entries):
                if index < max_files or sha256 in protected or now - modified < min_age:
                    continue
                try:
                    os.remove(path)
                    deleted += 1
                except OSError as err:
                    if err.errno != errno.ENOENT:
                        logging.warning("Could not remove cached variants.ini %s: %s", path, err)
    except IOError as err:
        logging.warning("Skipping variants.ini cache cleanup: %s", err)
        return 0

    if deleted:
        logging.info("Removed %d old cached variants.ini file(s)", deleted)
    return deleted


def _select_variants_ini(conf, expected_sha256=None, variant=None):
    entry = sync_variants_ini(conf, expected_sha256=expected_sha256, required=False, variant=variant)
    return entry or default_variants_ini(conf)


def _apply_engine_variants(p, entry):
    if p is not None:
        # Always apply the exact entry selected for this work unit. Another
        # worker thread may select a different hash at the same time.
        setoption(p, "VariantPath", entry.filename)
        isready(p)


def reload_engine_variants(p, conf, expected_sha256=None, variant=None):
    entry = _select_variants_ini(conf, expected_sha256, variant)
    _apply_engine_variants(p, entry)
    return entry


@contextlib.contextmanager
def use_engine_variants(p, conf, expected_sha256=None, variant=None):
    entry = _select_variants_ini(conf, expected_sha256, variant)
    with active_variants_ini(conf, entry):
        _apply_engine_variants(p, entry)
        yield entry


def pyffish_get_fen(entry, variant, fen, moves, chess960, sfen, show_promoted):
    with PYFFISH_VARIANT_LOCK:
        sf.set_option("VariantPath", entry.path)
        return sf.get_fen(variant, fen, moves, chess960, sfen, show_promoted)


def create_variants_ini(args):
    conf = load_conf(args)
    ini_text = textwrap.dedent("""\
# Hybrid variant of Grand-chess and crazyhouse, using Grand-chess as a template
[grandhouse:grand]
startFen = r8r/1nbqkcabn1/pppppppppp/10/10/10/10/PPPPPPPPPP/1NBQKCABN1/R8R[] w - - 0 1
pieceDrops = true
capturesToHand = true

# Hybrid variant of Gothic-chess and crazyhouse, using Capablanca as a template
[gothhouse:capablanca]
startFen = rnbqckabnr/pppppppppp/10/10/10/10/PPPPPPPPPP/RNBQCKABNR[] w KQkq - 0 1
pieceDrops = true
capturesToHand = true

# Hybrid variant of Embassy chess and crazyhouse, using Embassy as a template
[embassyhouse:embassy]
startFen = rnbqkcabnr/pppppppppp/10/10/10/10/PPPPPPPPPP/RNBQKCABNR[] w KQkq - 0 1
pieceDrops = true
capturesToHand = true

[gorogoroplus:gorogoro]
startFen = sgkgs/5/1ppp1/1PPP1/5/SGKGS[LNln] w 0 1
lance = l
shogiKnight = n
promotedPieceType = l:g n:g

[cannonshogi:shogi]
# No Shogi pawn drop restrictions
dropNoDoubled = -
shogiPawnDropMateIllegal = false
# Soldier is Janggi soldier
soldier = p
# Gold Cannon is exactly like Xiangqi cannon
cannon = u
# Silver Cannon moves and captures like Janggi cannon
# Janggi cannons have this EXCEPTION:
# The cannon cannot use another cannon as a screen. Additionally, it can't capture the opponent's cannons.
# This is NOT exists here.
customPiece1 = a:pR
# Copper Cannon is diagonal Xiangqi cannon
customPiece2 = c:mBcpB
# Iron Cannon is diagonal Janggi cannon
customPiece3 = i:pB
# Flying Silver/Gold Cannon
customPiece4 = w:mRpRmFpB2
# Flying Copper/Iron Cannon
customPiece5 = f:mBpBmWpR2
promotedPieceType = u:w a:w c:f i:f p:g
startFen = lnsgkgsnl/1rci1uab1/p1p1p1p1p/9/9/9/P1P1P1P1P/1BAU1ICR1/LNSGKGSNL[-] w 0 1

[shogun:crazyhouse]
startFen = rnb+fkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB+FKBNR[] w KQkq - 0 1
commoner = c
centaur = g
archbishop = a
chancellor = m
fers = f
promotionRegionWhite = *6 *7 *8
promotionRegionBlack = *3 *2 *1
promotionLimit = g:1 a:1 m:1 q:1
promotionPieceTypes = -
promotedPieceType = p:c n:g b:a r:m f:q
mandatoryPawnPromotion = false
firstRankPawnDrops = true
promotionZonePawnDrops = true
whiteDropRegion = *1 *2 *3 *4 *5
blackDropRegion = *4 *5 *6 *7 *8
immobilityIllegal = true

[orda:chess]
centaur = h
knibis = a
kniroo = l
silver = y
promotionPieceTypes = qh
startFen = lhaykahl/8/pppppppp/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[khans:chess]
centaur = h
knibis = a
kniroo = l
customPiece1 = t:mNcK
customPiece2 = s:mfhNcfW
promotionPawnTypesBlack = s
promotionPieceTypesBlack = t
stalemateValue = loss
nMoveRuleTypesBlack = s
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1
startFen = lhatkahl/ssssssss/8/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1

[synochess:pocketknight]
janggiCannon = c
soldier = s
horse = h
fersAlfil = e
commoner = a
startFen = rneakenr/8/1c4c1/1ss2ss1/8/8/PPPPPPPP/RNBQKBNR[ss] w KQ - 0 1
stalemateValue = loss
perpetualCheckIllegal = true
flyingGeneral = true
blackDropRegion = *5
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[shinobi:crazyhouse]
commoner = c
bers = d
archbishop = j
fers = m
shogiKnight = h
lance = l
promotionRegionWhite = *7 *8
promotionRegionBlack = *2 *1
promotionPieceTypes = -
promotedPieceType = p:c m:b h:n l:r
mandatoryPiecePromotion = true
stalemateValue = loss
nFoldRule = 4
perpetualCheckIllegal = true
startFen = rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/LH1CK1HL[LHMMDJ] w kq - 0 1
capturesToHand = false
whiteDropRegion = *1 *2 *3 *4
immobilityIllegal = true
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[shinobiplus:crazyhouse]
commoner = c
bers = d
dragonHorse = f
archbishop = j
fers = m
shogiKnight = h
lance = l
promotionRegionWhite = *7 *8
promotionRegionBlack = *1 *2 *3
promotionPieceTypes = -
promotedPieceType = p:c m:b h:n l:r
mandatoryPiecePromotion = true
stalemateValue = loss
nFoldRule = 4
perpetualCheckIllegal = true
startFen = rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/4K3[JDFCLHM] w kq - 0 1
capturesToHand = false
whiteDropRegion = *1 *2 *3 *4
immobilityIllegal = true
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[ordamirror:chess]
centaur = h
knibis = a
kniroo = l
customPiece1 = f:mQcN
promotionPieceTypes = lhaf
startFen = lhafkahl/8/pppppppp/8/8/PPPPPPPP/8/LHAFKAHL w - - 0 1
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1

[empire:chess]
customPiece1 = e:mQcN
customPiece2 = c:mQcB
customPiece3 = t:mQcR
customPiece4 = d:mQcK
soldier = s
promotionPieceTypes = q
startFen = rnbqkbnr/pppppppp/8/8/8/PPPSSPPP/8/TECDKCET w kq - 0 1
stalemateValue = loss
nFoldValue = win
flagPiece = k
flagRegionWhite = *8
flagRegionBlack = *1
flyingGeneral = true

[chak]
maxRank = 9
maxFile = 9
rook = r
knight = v
centaur = j
immobile = o
customPiece1 = s:FvW
customPiece2 = q:pQ
customPiece3 = d:mQ2cQ2
customPiece4 = p:fsmWfceF
customPiece5 = k:WF
customPiece6 = w:FvW
startFen = rvsqkjsvr/4o4/p1p1p1p1p/9/9/9/P1P1P1P1P/4O4/RVSJKQSVR w - - 0 1
mobilityRegionWhiteCustomPiece6 = *5 *6 *7 *8 *9
mobilityRegionWhiteCustomPiece3 = *5 *6 *7 *8 *9
mobilityRegionBlackCustomPiece6 = *1 *2 *3 *4 *5
mobilityRegionBlackCustomPiece3 = *1 *2 *3 *4 *5
promotionRegionWhite = *5 *6 *7 *8 *9
promotionRegionBlack = *5 *4 *3 *2 *1
promotionPieceTypes = -
mandatoryPiecePromotion = true
promotedPieceType = p:w k:d
extinctionValue = loss
extinctionPieceTypes = kd
extinctionPseudoRoyal = true
flagPiece = d
flagRegionWhite = e8
flagRegionBlack = e2
nMoveRule = 50
nFoldRule = 3
nFoldValue = draw
stalemateValue = loss

[chennis]
maxRank = 7
maxFile = 7
mobilityRegionWhiteKing = b1 c1 d1 e1 f1 b2 c2 d2 e2 f2 b3 c3 d3 e3 f3 b4 c4 d4 e4 f4
mobilityRegionBlackKing = b4 c4 d4 e4 f4 b5 c5 d5 e5 f5 b6 c6 d6 e6 f6 b7 c7 d7 e7 f7
customPiece1 = p:fmWfceF
cannon = c
commoner = m
fers = f
soldier = s
king = k
bishop = b
knight = n
rook = r
promotionPieceTypes = -
promotedPieceType = p:r f:c s:b m:n
promotionRegionWhite = *1 *2 *3 *4 *5 *6 *7
promotionRegionBlack = *7 *6 *5 *4 *3 *2 *1
startFen = 1fkm3/1p1s3/7/7/7/3S1P1/3MKF1[] w - 0 1
pieceDrops = true
capturesToHand = true
pieceDemotion = true
mandatoryPiecePromotion = true
dropPromoted = true
castling = false
stalemateValue = loss

# Mansindam (Pantheon tale)
# A variant that combines drop rule and powerful pieces, and there is no draw
[mansindam]
variantTemplate = shogi
pieceToCharTable = PNBR.Q.CMA.++++...++Kpnbr.q.cma.++++...++k
maxFile = 9
maxRank = 9
pocketSize = 8
startFen = rnbakqcnm/9/ppppppppp/9/9/9/PPPPPPPPP/9/MNCQKABNR[] w - - 0 1
pieceDrops = true
capturesToHand = true
shogiPawn = p
knight = n
bishop = b
rook = r
queen = q
archbishop = c
chancellor = m
amazon = a
king = k
commoner = g
centaur = e
dragonHorse = h
bers = t
customPiece1 = i:BNW
customPiece2 = s:RNF
promotionRegionWhite = *7 *8 *9
promotionRegionBlack = *3 *2 *1
mandatoryPiecePromotion = true
doubleStep = false
castling = false
promotedPieceType = p:g n:e b:h r:t c:i m:s
dropNoDoubled = p
stalemateValue = loss
nMoveRule = 0
nFoldValue = win
flagPiece = k
flagRegionWhite = *9
flagRegionBlack = *1
immobilityIllegal = true

[fogofwar:chess]
king = -
commoner = k
castlingKingPiece = k
extinctionValue = loss
extinctionPieceTypes = k

# Hybrid variant of xiangqi and crazyhouse
[xiangqihouse:xiangqi]
startFen = rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR[] w - - 0 1
pieceDrops = true
capturesToHand = true
dropChecks = false
whiteDropRegion = *1 *2 *3 *4 *5
blackDropRegion = *6 *7 *8 *9 *10
mobilityRegionWhiteFers = d1 f1 e2 d3 f3
mobilityRegionBlackFers = d8 f8 e9 d10 f10
mobilityRegionWhiteElephant = c1 g1 a3 e3 i3 c5 g5
mobilityRegionBlackElephant = c6 g6 a8 e8 i8 c10 g10
mobilityRegionWhiteSoldier = a4 a5 c4 c5 e4 e5 g4 g5 i4 i5 *6 *7 *8 *9 *10
mobilityRegionBlackSoldier = *1 *2 *3 *4 *5 a6 a7 c6 c7 e6 e7 g6 g7 i6 i7

# Hybrid variant of makruk and crazyhouse
[makrukhouse:makruk]
startFen = rnsmksnr/8/pppppppp/8/8/PPPPPPPP/8/RNSKMSNR[] w - - 0 1
pieceDrops = true
capturesToHand = true
firstRankPawnDrops = true
promotionZonePawnDrops = true
immobilityIllegal = true

[makbug:makrukhouse]
startFen = rnsmksnr/8/pppppppp/8/8/PPPPPPPP/8/RNSKMSNR[] w - - 0 1
capturesToHand = false
twoBoards = true

# Martial arts Xiangqi
[xiangfu]
maxFile = 9
maxRank = 9
startFen = 2rbm4/2cwn4/2+g1+g4/9/9/9/4+G1+G2/4NWC2/4MBR2[] w - 0 1
commoner = k
bishop = b
horse = n
rook = r
cannon = c
customPiece1 = w:mBcpB
customPiece2 = m:nAnD
customPiece3 = g:Q1
mobilityRegionBlackCommoner = c3 c4 c5 c6 c7 d3 d4 d5 d6 d7 e3 e4 e5 e6 e7 f3 f4 f5 f6 f7 g3 g4 g5 g6 g7
mobilityRegionWhiteCommoner = c3 c4 c5 c6 c7 d3 d4 d5 d6 d7 e3 e4 e5 e6 e7 f3 f4 f5 f6 f7 g3 g4 g5 g6 g7
pieceDrops = true
capturesToHand = true
whiteDropRegion = *1 *2
blackDropRegion = *8 *9
extinctionPieceTypes = k
extinctionPseudoRoyal = true
dupleCheck = true
promotedPieceType = g:k
promotionRegionWhite = -
promotionRegionBlack = -

[borderlands]
maxFile = 9
maxRank = 10
# Non-promoting pieces.
customPiece1 = c:K
customPiece2 = g:K
# Unpromoted pieces.
customPiece3 = a:RcpR
customPiece4 = s:BcpB
customPiece5 = h:NF
customPiece6 = e:ADW
customPiece7 = m:F
customPiece8 = f:W
customPiece9 = w:fWfceFifmnD
customPiece10 = l:KNAD
# Promoted pieces.
customPiece11 = b:RFcpR
customPiece12 = d:BWcpB
customPiece13 = i:NK
customPiece14 = j:ADK
customPiece15 = k:KNAD
promotedPieceType = a:b s:d h:i e:j m:g f:g w:i l:k
mandatoryPiecePromotion = true
startFen = a3s3a/1chesehc1/fw1wlw1wf/w1w1w1w1w/9/9/W1W1W1W1W/FW1WLW1WF/1CHESEHC1/A3S3A[MMmm] w - - 0 1
mobilityRegionWhiteCustomPiece10 = *1 *2 *3 *4 *5 d7 f7 e9
mobilityRegionBlackCustomPiece10 = *6 *7 *8 *9 *10 d4 f4 e2
pieceDrops = true
capturesToHand = false
whiteDropRegion = *6 *7
blackDropRegion = *4 *5
promotionRegionWhite = *8 *9 *10
promotionRegionBlack = *1 *2 *3
doubleStepRegionWhite = *3
doubleStepRegionBlack = *8
nMoveRule = 40
perpetualCheckIllegal = true
moveRepetitionIllegal = true
nFoldRule = 4
extinctionValue = loss
extinctionPseudoRoyal = false
extinctionPieceTypes = c
extinctionPieceCount = 0
""")

    write_variants_ini(conf, ini_text)
