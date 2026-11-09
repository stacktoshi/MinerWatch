# SPDX-License-Identifier: AGPL-3.0-only
"""Driver for the GekkoScience KBox (Solo Mining Co firmware).

The KBox is a standalone BM1370 heater/miner with a Flask web UI on
port 80. Monitoring and control go through a local JSON API under
``/api/v1/*``, authenticated with the ``X-API-Key`` header (Settings →
System → API Access on the box). The key is read from ``KBOX_API_KEY``
in the process environment, or from a gitignored repo-root ``.env``.

Poll is ``GET /api/v1/status`` plus ``GET /api/v1/pools``. Hashrate is
already TH/s (do not divide by 1000). There is no watts field and no
MAC — identity is the host. ``hardware_errors`` is ignored: live
firmware has been seen reporting a ~400M counter that is not a real
ASIC error count.

Controls: fan (percent or auto), power mode Low/Medium/High (mapped
onto the existing workmode 0/1/2 UI), restart. Pool writes and LEDs
are out of scope — v1 of the KBox API keeps pool config owner-only.

Reference: the unit's own ``/apidocs`` (firmware v1.19.4).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from ..config import ROOT_DIR
from .base import MinerDriver, MinerSample, PoolSnapshot, parse_si_difficulty


_POWER_MODES = ("Low", "Medium", "High")
_POWER_MODE_INDEX = {name.lower(): i for i, name in enumerate(_POWER_MODES)}


class KboxDriver(MinerDriver):
    family = "kbox"
    DEFAULT_PORT = 80

    can_set_fan = True
    can_set_frequency = False
    can_set_voltage = False
    can_set_workmode = True
    can_restart = True
    can_pause = False
    can_shutdown = False
    can_set_pool = False

    def _base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def poll(self) -> MinerSample:
        if not kbox_api_key():
            return MinerSample(
                family=self.family,
                host=self.host,
                online=False,
                error="KBOX_API_KEY is not set",
            )
        status = await self._get("/api/v1/status")
        if not isinstance(status, dict) or not status.get("ok"):
            err = None
            if isinstance(status, dict):
                err = status.get("message") or status.get("error")
            return MinerSample(
                family=self.family,
                host=self.host,
                online=False,
                error=str(err) if err else "KBox status request failed",
            )
        pools = await self._get("/api/v1/pools")
        return self._parse(status, pools if isinstance(pools, dict) else None)

    async def fetch_login_html(self) -> str:
        """GET ``/login`` — public, used by discovery to fingerprint the family."""
        url = f"{self._base_url()}/login"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.get(url)
                resp.raise_for_status()
                return resp.text or ""
        except (httpx.HTTPError, ValueError):
            return ""

    def _parse(
        self,
        status: dict[str, Any],
        pools: dict[str, Any] | None = None,
    ) -> MinerSample:
        hashrate_ths = _opt_float(status.get("hashrate_ths"))
        if hashrate_ths is None:
            hashrate_ths = _opt_float(status.get("hashrate_1m_ths"))

        pool = status.get("pool") if isinstance(status.get("pool"), dict) else {}
        pool_url = pool.get("url") if isinstance(pool.get("url"), str) else None
        worker = pool.get("user") if isinstance(pool.get("user"), str) else None

        snapshots, fallback_url, fallback_user = _pools_from_payload(pools)
        if not snapshots and pool_url:
            snapshots = [
                PoolSnapshot(
                    url=pool_url,
                    user=worker,
                    status="alive",
                    active=True,
                    slot="primary",
                    accepted=_opt_int(status.get("accepted")),
                    rejected=_opt_int(status.get("rejected")),
                )
            ]

        sample = MinerSample(
            family=self.family,
            host=self.host,
            online=True,
            mac=None,
            model="KBox",
            chip_model="BM1370",
            hashrate_ths=hashrate_ths,
            temp_chip_c=_opt_float(status.get("temperature_c")),
            fan_pct=_opt_float(status.get("fan_percent")),
            uptime_s=_opt_int(status.get("uptime_s")),
            accepted=_opt_int(status.get("accepted")),
            rejected=_opt_int(status.get("rejected")),
            best_difficulty=parse_si_difficulty(status.get("best_share")),
            workmode=_workmode_from_power(status.get("power_mode")),
            asic_count=1,
            board_count=1,
            pool_url=pool_url,
            worker=worker,
            pool_url_fallback=fallback_url,
            worker_fallback=fallback_user,
            pools=snapshots,
            raw=status,
        )
        return sample

    async def set_fan_speed(self, percent: int) -> bool:
        percent = int(percent)
        if percent <= 0:
            return await self.set_auto_fan(True)
        percent = max(1, min(100, percent))
        resp = await self._post("/api/v1/fan", {"percent": percent})
        return bool(resp and resp.get("ok"))

    async def set_auto_fan(self, enabled: bool = True) -> bool:
        if not enabled:
            return False
        resp = await self._post("/api/v1/fan", {"mode": "auto"})
        return bool(resp and resp.get("ok"))

    async def set_workmode(self, mode: int) -> bool:
        if mode not in (0, 1, 2):
            return False
        resp = await self._post("/api/v1/power", {"mode": _POWER_MODES[mode]})
        return bool(resp and resp.get("ok"))

    async def restart(self) -> bool:
        resp = await self._post("/api/v1/restart", {})
        return bool(resp and resp.get("ok"))

    async def _get(self, path: str) -> dict[str, Any] | None:
        return await self._request("GET", path)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return await self._request("POST", path, body)

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        key = kbox_api_key()
        if not key:
            return None
        url = f"{self._base_url()}{path}"
        headers = {"X-API-Key": key}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.request(
                    method,
                    url,
                    headers=headers,
                    json=body if method.upper() != "GET" else None,
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) else None


def kbox_api_key(*, dotenv_path: Path | None = None) -> str | None:
    """Return the KBox API key from the environment or a ``.env`` file."""
    env = (os.environ.get("KBOX_API_KEY") or "").strip()
    if env:
        return env
    path = ROOT_DIR / ".env" if dotenv_path is None else dotenv_path
    return _read_dotenv_key(path)


def login_html_is_kbox(html: str) -> bool:
    """True when public ``/login`` HTML is the GekkoScience KBox sign-in page."""
    return "gekkoscience kbox" in (html or "").lower()


def _read_dotenv_key(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != "KBOX_API_KEY":
            continue
        key = value.strip().strip("'").strip('"')
        return key or None
    return None


def _workmode_from_power(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _POWER_MODE_INDEX.get(value.strip().lower())


def _pools_from_payload(
    payload: dict[str, Any] | None,
) -> tuple[list[PoolSnapshot], str | None, str | None]:
    if not isinstance(payload, dict):
        return [], None, None
    rows = payload.get("pools")
    if not isinstance(rows, list):
        return [], None, None
    entries: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = _opt_int(row.get("index"))
        entries.append((idx if idx is not None else len(entries), row))
    entries.sort(key=lambda item: item[0])

    snapshots: list[PoolSnapshot] = []
    fallback_url: str | None = None
    fallback_user: str | None = None
    for i, (_, row) in enumerate(entries):
        url = row.get("url") if isinstance(row.get("url"), str) else None
        user = row.get("user") if isinstance(row.get("user"), str) else None
        status_raw = row.get("status")
        status = None
        if isinstance(status_raw, str) and status_raw.strip():
            status = status_raw.strip().lower()
        snap = PoolSnapshot(
            url=url,
            user=user,
            status=status,
            accepted=_opt_int(row.get("accepted")),
            rejected=_opt_int(row.get("rejected")),
            active=bool(row.get("active")) if row.get("active") is not None else None,
            slot="primary" if i == 0 else "fallback",
        )
        snapshots.append(snap)
        if i == 1:
            fallback_url = url
            fallback_user = user
    return snapshots, fallback_url, fallback_user


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
