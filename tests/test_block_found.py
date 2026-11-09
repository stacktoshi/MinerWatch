# SPDX-License-Identifier: AGPL-3.0-only
"""False-positive guards for the block-found detector.

Two field bugs from a real fleet (2026-09):

  * A KBox reported ``best_share = 2**64-1`` (uninitialized uint64).
    Compared to Bitcoin's ~127 T network difficulty that looks like a
    block, but it is a firmware sentinel, not a share.
  * A Bitaxe reported ``networkDifficulty = 1.94e6`` (pool/job target)
    while a 54 M share was sitting in ``bestSessionDiff``. Trusting the
    miner-reported network difficulty fired a block against a number
    that is not a chain difficulty at all.

Runs under pytest, or standalone: ``python tests/test_block_found.py``.
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.miners.base import parse_si_difficulty  # noqa: E402
from backend.miners.kbox import KboxDriver  # noqa: E402
from backend.poller import trusted_network_difficulty  # noqa: E402

UINT64_MAX = 2**64 - 1
REFS = {"btc": 1.2745e14, "bch": 4.0e11}


def test_parse_si_drops_uint64_max_sentinel():
    """A production change that stopped dropping 2**64-1 would re-fire
    the KBox false block (share 18.45 E vs network 127 T)."""
    assert parse_si_difficulty(UINT64_MAX) is None
    assert parse_si_difficulty(float(UINT64_MAX)) is None
    assert parse_si_difficulty(54_267_952) == 54_267_952.0


def test_kbox_uint64_best_share_is_absent():
    status = {
        "ok": True,
        "hashrate_ths": 16.4,
        "best_share": UINT64_MAX,
        "accepted": 1,
        "rejected": 0,
        "power_mode": "Medium",
        "temperature_c": 60,
        "fan_percent": 40,
        "uptime_s": 10,
        "pool": {"url": "stratum+tcp://solo.ckpool.org:3333", "user": "bc1qxyz.Kbox"},
    }
    sample = KboxDriver("10.0.0.8")._parse(status)
    assert sample.best_difficulty is None


def test_trusted_network_difficulty_rejects_pool_target():
    """1.94 M is a stratum job target, not BTC (~127 T) or BCH (~400 G)."""
    sample = SimpleNamespace(network_difficulty=1_944_219.0)
    miner = {"coin_override": None, "family": "bitaxe"}
    assert trusted_network_difficulty(sample, miner, REFS) is None


def test_trusted_network_difficulty_accepts_real_btc():
    sample = SimpleNamespace(
        network_difficulty=127_450_789_715_843.0,
        online=True,
        pool_url=None,
        worker=None,
    )
    miner = {"coin_override": None, "family": "bitaxe"}
    got = trusted_network_difficulty(sample, miner, REFS)
    assert got == 127_450_789_715_843.0


def test_trusted_network_difficulty_falls_back_to_address_coin():
    """When the miner reports no (trusted) network diff, a bc1 worker is BTC."""
    sample = SimpleNamespace(
        network_difficulty=None,
        online=True,
        pool_url="stratum+tcp://solo.ckpool.org:3333",
        worker="bc1qxyz.Kbox",
    )
    miner = {"coin_override": None, "family": "kbox"}
    assert trusted_network_difficulty(sample, miner, REFS) == REFS["btc"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
