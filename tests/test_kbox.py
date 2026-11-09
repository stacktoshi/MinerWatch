# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the GekkoScience KBox driver.

The KBox speaks a password-gated Flask UI on port 80 plus a local JSON
API under ``/api/v1/*`` authenticated with ``X-API-Key``. These tests
pin:

  * status/pool field mapping (TH/s already, not GH/s)
  * Low/Medium/High → workmode 0/1/2
  * skipping the firmware's garbage ``hardware_errors`` counter
  * fan / power-mode / restart write payloads
  * discovery via the public ``/login`` page
  * ``KBOX_API_KEY`` from the environment or a gitignored ``.env``

Fixtures are redacted from a live KBox on firmware v1.19.4. Runs under
pytest, or standalone: ``python tests/test_kbox.py``.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import discovery  # noqa: E402
from backend.miners import DRIVERS, get_driver  # noqa: E402
from backend.miners.kbox import (  # noqa: E402
    KboxDriver,
    kbox_api_key,
    login_html_is_kbox,
)


# Live ``GET /api/v1/status`` shape (wallet redacted). Hashrate is already
# TH/s; ``hardware_errors`` is the implausible counter we must not surface.
_STATUS = {
    "accepted": 92301,
    "best_share": 330185291,
    "dual_mining": True,
    "fan_percent": 50,
    "hardware_errors": 399930240,
    "hashrate_1m_ths": 16.479,
    "hashrate_ths": 16.439,
    "led": {
        "brightness": 72,
        "color": {"b": 0, "g": 0, "r": 207},
        "effect": "Woodstove",
        "on": True,
        "speed": 842,
    },
    "ok": True,
    "pool": {
        "difficulty": 3837.0,
        "url": "stratum+tcp://solo.example.com:3333",
        "user": "bc1qxyz.Kbox",
    },
    "power_mode": "Medium",
    "rejected": 81,
    "temperature_c": 68.8,
    "uptime_s": 249040,
}

_POOLS = {
    "ok": True,
    "pools": [
        {
            "accepted": 67938,
            "active": True,
            "index": 0,
            "quota": 30,
            "rejected": 55,
            "status": "Alive",
            "url": "stratum+tcp://solo.example.com:3333",
            "user": "bc1qxyz.Kbox",
            "work_difficulty": 3837.0,
        },
        {
            "accepted": 24376,
            "active": True,
            "index": 1,
            "quota": 70,
            "rejected": 26,
            "status": "Alive",
            "url": "stratum+tcp://pool.example.com:3333",
            "user": "worker.kbox",
            "work_difficulty": 32768.0,
        },
    ],
}

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>KBox &mdash; Sign in</title></head>
<body>
  <div class="logo">GekkoScience KBox</div>
  <div class="sub">Sign in to continue</div>
</body>
</html>
"""


def _parse(status=None, pools=None):
    return KboxDriver("10.0.0.8")._parse(status or _STATUS, pools)


# ---- registry / capabilities ---------------------------------------


def test_family_registered():
    assert get_driver("kbox") is KboxDriver
    assert DRIVERS["kbox"] is KboxDriver
    assert KboxDriver.family == "kbox"


def test_capabilities_fan_workmode_restart():
    assert KboxDriver.can_set_fan is True
    assert KboxDriver.can_set_workmode is True
    assert KboxDriver.can_restart is True
    assert KboxDriver.can_set_frequency is False
    assert KboxDriver.can_set_voltage is False
    assert KboxDriver.can_set_pool is False
    assert KboxDriver.can_pause is False
    assert KboxDriver.can_shutdown is False


# ---- _parse --------------------------------------------------------


def test_parse_core_fields():
    s = _parse()
    assert s.family == "kbox"
    assert s.online is True
    assert s.mac is None
    assert s.model == "KBox"
    assert s.chip_model == "BM1370"
    assert s.hashrate_ths == 16.439
    assert s.power_w is None
    assert s.efficiency_w_per_ths is None
    assert s.temp_chip_c == 68.8
    assert s.fan_pct == 50
    assert s.fan_rpm is None
    assert s.uptime_s == 249040
    assert s.accepted == 92301
    assert s.rejected == 81
    assert s.workmode == 1
    assert s.pool_url == "stratum+tcp://solo.example.com:3333"
    assert s.worker == "bc1qxyz.Kbox"


def test_parse_does_not_treat_hashrate_as_ghs():
    """KBox reports TH/s already — dividing by 1000 would show 0.016 TH/s."""
    s = _parse()
    assert s.hashrate_ths > 1


def test_parse_skips_garbage_hardware_errors():
    s = _parse()
    assert s.hw_errors is None
    assert s.hw_error_rate is None


def test_parse_best_share_numeric():
    s = _parse()
    assert s.best_difficulty == 330185291.0


def test_parse_best_share_si_string():
    status = dict(_STATUS)
    status["best_share"] = "2.23T"
    s = _parse(status=status)
    assert s.best_difficulty == 2.23e12


def test_parse_workmode_low_high():
    low = dict(_STATUS, power_mode="Low")
    high = dict(_STATUS, power_mode="High")
    assert _parse(status=low).workmode == 0
    assert _parse(status=high).workmode == 2


def test_parse_startup_nulls():
    status = dict(_STATUS)
    status["hashrate_ths"] = None
    status["hashrate_1m_ths"] = None
    status["temperature_c"] = None
    s = _parse(status=status)
    assert s.hashrate_ths is None
    assert s.temp_chip_c is None
    assert s.online is True


def test_parse_pools_dual():
    s = _parse(pools=_POOLS)
    assert len(s.pools) == 2
    assert s.pools[0].slot == "primary"
    assert s.pools[0].active is True
    assert s.pools[0].url == "stratum+tcp://solo.example.com:3333"
    assert s.pools[0].user == "bc1qxyz.Kbox"
    assert s.pools[0].accepted == 67938
    assert s.pools[1].slot == "fallback"
    assert s.pools[1].url == "stratum+tcp://pool.example.com:3333"
    assert s.pool_url_fallback == "stratum+tcp://pool.example.com:3333"
    assert s.worker_fallback == "worker.kbox"


def test_parse_synthesises_pool_from_status_when_pools_missing():
    s = _parse(pools=None)
    assert len(s.pools) == 1
    assert s.pools[0].slot == "primary"
    assert s.pools[0].url == "stratum+tcp://solo.example.com:3333"
    assert s.pools[0].active is True


# ---- poll / auth ---------------------------------------------------


def test_poll_without_api_key_is_offline():
    drv = KboxDriver("10.0.0.8")
    with patch("backend.miners.kbox.kbox_api_key", return_value=None):
        sample = asyncio.run(drv.poll())
    assert sample.online is False
    assert sample.family == "kbox"
    assert sample.error


def test_poll_maps_status_and_pools():
    drv = KboxDriver("10.0.0.8")
    get = AsyncMock(side_effect=[_STATUS, _POOLS])
    with patch("backend.miners.kbox.kbox_api_key", return_value="test-key"), \
         patch.object(drv, "_get", get):
        sample = asyncio.run(drv.poll())
    assert sample.online is True
    assert sample.hashrate_ths == 16.439
    assert len(sample.pools) == 2
    assert get.await_args_list[0].args[0] == "/api/v1/status"
    assert get.await_args_list[1].args[0] == "/api/v1/pools"


# ---- writes --------------------------------------------------------


def test_set_workmode_posts_named_mode():
    drv = KboxDriver("10.0.0.8")
    with patch.object(drv, "_post", AsyncMock(return_value={"ok": True})) as post:
        ok = asyncio.run(drv.set_workmode(2))
    assert ok is True
    post.assert_awaited_once_with("/api/v1/power", {"mode": "High"})


def test_set_workmode_rejects_out_of_range():
    drv = KboxDriver("10.0.0.8")
    with patch.object(drv, "_post", AsyncMock()) as post:
        assert asyncio.run(drv.set_workmode(3)) is False
        assert asyncio.run(drv.set_workmode(-1)) is False
    post.assert_not_awaited()


def test_set_fan_speed_posts_percent():
    drv = KboxDriver("10.0.0.8")
    with patch.object(drv, "_post", AsyncMock(return_value={"ok": True})) as post:
        ok = asyncio.run(drv.set_fan_speed(60))
    assert ok is True
    post.assert_awaited_once_with("/api/v1/fan", {"percent": 60})


def test_set_fan_speed_clamps_and_zero_is_auto():
    drv = KboxDriver("10.0.0.8")
    with patch.object(drv, "_post", AsyncMock(return_value={"ok": True})) as post:
        asyncio.run(drv.set_fan_speed(150))
        asyncio.run(drv.set_fan_speed(0))
    assert post.await_args_list[0].args == ("/api/v1/fan", {"percent": 100})
    assert post.await_args_list[1].args == ("/api/v1/fan", {"mode": "auto"})


def test_set_auto_fan():
    drv = KboxDriver("10.0.0.8")
    with patch.object(drv, "_post", AsyncMock(return_value={"ok": True})) as post:
        ok = asyncio.run(drv.set_auto_fan(True))
    assert ok is True
    post.assert_awaited_once_with("/api/v1/fan", {"mode": "auto"})


def test_restart_posts_restart():
    drv = KboxDriver("10.0.0.8")
    with patch.object(drv, "_post", AsyncMock(return_value={"ok": True})) as post:
        ok = asyncio.run(drv.restart())
    assert ok is True
    post.assert_awaited_once_with("/api/v1/restart", {})


# ---- API key -------------------------------------------------------


def test_api_key_prefers_environment():
    with patch.dict(os.environ, {"KBOX_API_KEY": "from-env-key"}, clear=False):
        assert kbox_api_key(dotenv_path=pathlib.Path("/no/such/.env")) == "from-env-key"


def test_api_key_reads_dotenv_when_env_empty():
    fd, name = tempfile.mkstemp(prefix="kbox", suffix=".env")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("KBOX_API_KEY=dotenv-secret-key\n")
        env = {k: v for k, v in os.environ.items() if k != "KBOX_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            assert kbox_api_key(dotenv_path=pathlib.Path(name)) == "dotenv-secret-key"
    finally:
        os.unlink(name)


def test_api_key_missing_is_none():
    env = {k: v for k, v in os.environ.items() if k != "KBOX_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        assert kbox_api_key(dotenv_path=pathlib.Path("/no/such/.env")) is None


# ---- discovery -----------------------------------------------------


def test_login_html_is_kbox():
    assert login_html_is_kbox(_LOGIN_HTML) is True
    assert login_html_is_kbox("<html><title>AxeOS</title></html>") is False
    assert login_html_is_kbox("") is False


def test_identify_kbox_from_login_page():
    with patch.object(KboxDriver, "fetch_login_html", AsyncMock(return_value=_LOGIN_HTML)):
        info = asyncio.run(discovery._identify_kbox("10.0.0.8"))
    assert info is not None
    assert info["family"] == "kbox"
    assert info["host"] == "10.0.0.8"
    assert info["port"] == discovery.PORT_BITAXE
    assert info["mac"] is None
    assert info["model"] == "KBox"
    assert info["name"] == "KBox 10.0.0.8"


def test_identify_kbox_absent_on_other_web_ui():
    with patch.object(
        KboxDriver, "fetch_login_html",
        AsyncMock(return_value="<html>Bitaxe</html>"),
    ):
        info = asyncio.run(discovery._identify_kbox("10.0.0.8"))
    assert info is None


def test_identify_from_ports_runs_kbox_before_bitaxe():
    """A KBox 401 on /api/system/info must not be dropped as 'not a miner'."""
    kbox = {
        "family": "kbox", "host": "10.0.0.8", "port": 80,
        "mac": None, "model": "KBox", "name": "KBox 10.0.0.8",
    }
    with patch.object(discovery, "_identify_nmaxe", AsyncMock(return_value=None)), \
         patch.object(discovery, "_identify_kbox", AsyncMock(return_value=kbox)), \
         patch.object(discovery, "_identify_bitaxe", AsyncMock(return_value={"family": "bitaxe"})):
        info = asyncio.run(discovery._identify_from_ports("10.0.0.8", [discovery.PORT_BITAXE]))
    assert info is not None
    assert info["family"] == "kbox"


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
