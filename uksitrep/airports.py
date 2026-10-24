"""Airports: arrivals and departures boards from AeroDataBox (RapidAPI), fetched on click.

The free plan is metered in API units (400 a month, hard limit) and the airport arrivals/departures
call is a 2-unit "Tier 2" endpoint, so every board is rationed: cached for BOARD_TTL_S, capped per UTC day
(AERODATABOX_DAILY_BOARDS) and per month (AERODATABOX_MONTHLY_UNITS, kept under the plan's 400). Usage is
saved in DATA_DIR so a restart doesn't forget it. Past a cap the board says so and the map falls back to
the aircraft the ADS-B feed shows near the airport.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import aiohttp
from . import USER_AGENT

_LOGGER = logging.getLogger("uksitrep")

ADB_HOST = "aerodatabox.p.rapidapi.com"
ADB_URL = "https://" + ADB_HOST + "/flights/airports/icao/{icao}/{start}/{end}"
ICAO_RE = re.compile(r"^[A-Z]{4}$")
UK_TZ = ZoneInfo("Europe/London")
ADB_TIMEOUT = aiohttp.ClientTimeout(total=20)
BOARD_TTL_S = 30 * 60
BOARD_UNITS = 2          # FIDS is a Tier 2 endpoint
WINDOW_BEFORE_H, WINDOW_AFTER_H = 1, 11   # the endpoint allows at most 12 h per call
ROWS = 14


def _local_hhmm(t: dict | None) -> str | None:
    s = (t or {}).get("local") or ""
    return s[11:16] or None


def _row(f: dict) -> dict:
    mv = f.get("movement") or {}
    ap = mv.get("airport") or {}
    sched, rev = mv.get("scheduledTime"), mv.get("revisedTime") or mv.get("predictedTime")
    late = None
    try:
        if sched and rev:
            late = round((datetime.fromisoformat(rev["utc"].replace("Z", "+00:00")) -
                          datetime.fromisoformat(sched["utc"].replace("Z", "+00:00"))).total_seconds() / 60)
    except (KeyError, ValueError):
        pass
    return {
        "time": _local_hhmm(sched), "est": _local_hhmm(rev), "late": late if late and abs(late) >= 5 else None,
        "flight": (f.get("number") or "").strip(), "airline": (f.get("airline") or {}).get("name"),
        "place": ap.get("name") or ap.get("icao") or "?", "iata": ap.get("iata"),
        "status": f.get("status") or "", "terminal": mv.get("terminal"), "gate": mv.get("gate"),
        "aircraft": (f.get("aircraft") or {}).get("model"), "cancelled": f.get("status") == "Canceled",
        "_t": (sched or {}).get("utc") or "",
    }


class Airports:
    """Mixin for Situation."""

    async def airports(self) -> dict:
        path = Path(__file__).with_name("data") / "airports.json"
        if not getattr(self, "_airports", None):
            self._airports = await asyncio.to_thread(lambda: json.loads(path.read_text()))
        return {"airports": self._airports, "board": bool(self.cfg.aerodatabox_key)}

    # ---------------------------------------------------------------- budget
    def _adb_state(self) -> dict:
        now = datetime.now(timezone.utc)
        st = getattr(self, "_adb", None)
        if st is None:
            st = {"day": "", "boards": 0, "month": "", "units": 0}
            try:
                st.update(json.loads((self.cfg.data_dir / "aerodatabox_usage.json").read_text()))
            except (OSError, ValueError):
                pass
            self._adb = st
        if st["day"] != now.strftime("%Y-%m-%d"):
            st["day"], st["boards"] = now.strftime("%Y-%m-%d"), 0
        if st["month"] != now.strftime("%Y-%m"):
            st["month"], st["units"] = now.strftime("%Y-%m"), 0
        return st

    def _adb_spend(self) -> None:
        st = self._adb_state()
        if st["boards"] >= self.cfg.aerodatabox_daily_boards:
            raise RuntimeError(f"daily_limit: {self.cfg.aerodatabox_daily_boards} airport boards a day used "
                               "(AERODATABOX_DAILY_BOARDS), resets at midnight UTC")
        if st["units"] + BOARD_UNITS > self.cfg.aerodatabox_monthly_units:
            raise RuntimeError(f"monthly_limit: {st['units']} of {self.cfg.aerodatabox_monthly_units} "
                               "AeroDataBox units used this month, resets on the 1st")
        st["boards"] += 1
        st["units"] += BOARD_UNITS
        try:
            (self.cfg.data_dir / "aerodatabox_usage.json").write_text(json.dumps(st))
        except OSError as err:
            _LOGGER.warning("Could not save AeroDataBox usage: %s", err)

    # ---------------------------------------------------------------- board
    async def airport_board(self, icao: str) -> dict:
        icao = (icao or "").upper()
        if not ICAO_RE.match(icao):
            return {"error": "bad airport code"}
        if not self.cfg.aerodatabox_key:
            return {"error": "no_key", "note": "Add AERODATABOX_KEY for airport boards"}

        async def fetch() -> dict:
            self._adb_spend()  # raises (so nothing is called) once a cap is reached
            now = datetime.now(UK_TZ)
            start = (now - timedelta(hours=WINDOW_BEFORE_H)).strftime("%Y-%m-%dT%H:%M")
            end = (now + timedelta(hours=WINDOW_AFTER_H)).strftime("%Y-%m-%dT%H:%M")
            d = await self._json(
                ADB_URL.format(icao=icao, start=start, end=end),
                params={"direction": "Both", "withLeg": "false", "withCancelled": "true", "withCodeshared": "false",
                        "withCargo": "false", "withPrivate": "false", "withLocation": "false"},
                headers={"x-rapidapi-key": self.cfg.aerodatabox_key, "x-rapidapi-host": ADB_HOST,
                         "User-Agent": USER_AGENT}, timeout=ADB_TIMEOUT)
            cutoff = (now - timedelta(minutes=20)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")

            def rows(kind: str) -> list[dict]:
                r = [_row(f) for f in d.get(kind) or [] if not f.get("isCargo")]
                r = [x for x in r if x["_t"] >= cutoff]  # drop what has already gone
                r.sort(key=lambda x: x["_t"])
                for x in r:
                    del x["_t"]
                return r[:ROWS]

            st = self._adb_state()
            return {"icao": icao, "departures": rows("departures"), "arrivals": rows("arrivals"),
                    "generated": datetime.now(timezone.utc).isoformat(),
                    "budget": {"boards_today": st["boards"], "daily": self.cfg.aerodatabox_daily_boards,
                               "units_month": st["units"], "monthly": self.cfg.aerodatabox_monthly_units}}

        return await self.cache.get(f"airport:{icao}", BOARD_TTL_S, fetch)
