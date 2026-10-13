"""Trains: live departure boards and National Rail disruptions (Rail Data Marketplace),
with station positions from NaPTAN.

- Live Departure Board (Darwin) gives the next departures for one station by its CRS code.
- The Disruptions API's latest bulletin lists current incidents and engineering works with
  the stations affected.
- NaPTAN (DfT, OGL) gives every station's position and CRS code; it is a 27 MB XML file, so
  a small extract is bundled and refreshed monthly in the background.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import io
import json
import logging
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger("uksitrep")

RDM = "https://api1.raildata.org.uk"
LDB_URL = RDM + "/1010-live-departure-board-dep1_2/LDBWS/api/20220120/GetDepBoardWithDetails/{crs}"
DISRUPTIONS_URL = RDM + "/1010-disruptions-experience-api-11_1/bulletins/getLatest"
NAPTAN_RAIL = "https://naptan.api.dft.gov.uk/v1/access-nodes?atcoAreaCodes=910&dataFormat=xml"
CRS_RE = re.compile(r"^[A-Z]{3}$")
UK_TZ = ZoneInfo("Europe/London")
RAIL_TIMEOUT = aiohttp.ClientTimeout(total=20)
BOARD_ROWS = 10
STATIONS_REFRESH_S = 30 * 86400
DISRUPTION_KINDS = ("Disruption", "Engineering Works")


def parse_naptan_rail(xml: bytes) -> list[dict]:
    """NaPTAN rail access nodes -> [{crs, name, lat, lon, tiploc}] (active stations with a CRS code)."""
    ns = ""
    out: dict[str, dict] = {}
    for _, el in ET.iterparse(io.BytesIO(xml), events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag != "StopPoint":
            continue
        if not ns:
            ns = el.tag[:-len("StopPoint")]
        crs = el.findtext(f".//{ns}CrsRef")
        lat, lon = el.findtext(f".//{ns}Latitude"), el.findtext(f".//{ns}Longitude")
        if el.get("Status", "active") == "active" and crs and lat and lon and crs not in out:
            name = el.findtext(f".//{ns}StationName") or el.findtext(f".//{ns}CommonName") or crs
            out[crs] = {"crs": crs, "name": re.sub(r"\s+Rail Station$", "", name).strip(),
                        "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                        "tiploc": el.findtext(f".//{ns}TiplocRef")}
        el.clear()
    return sorted(out.values(), key=lambda s: s["name"])


def _clean(s) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def _late_min(std: str | None, etd: str | None) -> int | None:
    """Minutes late from "12:06" / "12:19" (handles midnight); None unless etd is a time."""
    if not std or not etd or not re.fullmatch(r"\d\d:\d\d", etd) or not re.fullmatch(r"\d\d:\d\d", std):
        return None
    a, b = int(std[:2]) * 60 + int(std[3:]), int(etd[:2]) * 60 + int(etd[3:])
    return (b - a) % 1440 if (b - a) % 1440 < 720 else 0


def _local(s: str | None) -> str | None:
    """Bulletin times are UK local without a zone -> ISO UTC."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=UK_TZ).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


class Rail:
    """Mixin for Situation."""

    # ------------------------------------------------------------ stations
    async def rail_stations(self) -> dict:
        store = self.cfg.data_dir / "rail_stations.json"
        saved: dict = {"fetched": 0, "stations": []}
        for path in (store, Path(__file__).with_name("data") / "rail_stations.json"):
            try:
                saved = await asyncio.to_thread(lambda: json.loads(path.read_text()))
                break
            except (OSError, ValueError):
                continue
        due = time.time() - saved.get("fetched", 0) > STATIONS_REFRESH_S and time.time() > self.rail_retry_at
        if due and (not self.rail_task or self.rail_task.done()):
            self.rail_task = self.spawn(self._refresh_rail_stations(store))
        return {"stations": saved.get("stations", [])}

    async def _refresh_rail_stations(self, store: Path) -> None:
        try:
            async with self.session.get(NAPTAN_RAIL, timeout=aiohttp.ClientTimeout(total=300)) as r:
                r.raise_for_status()
                raw = await r.read()
            stations = await asyncio.to_thread(parse_naptan_rail, raw)
            if len(stations) < 1000:
                raise RuntimeError(f"only {len(stations)} stations")
            await asyncio.to_thread(lambda: store.write_text(json.dumps({"fetched": time.time(), "stations": stations},
                                                                        separators=(",", ":"))))
            self._crs_index = None
            _LOGGER.info("Rail stations refreshed: %d", len(stations))
        except Exception as err:  # noqa: BLE001
            self.rail_retry_at = time.time() + 6 * 3600
            _LOGGER.warning("Rail stations refresh failed (%s); retrying in 6 h", err)

    async def _crs(self) -> dict[str, dict]:
        if not getattr(self, "_crs_index", None):
            self._crs_index = {s["crs"]: s for s in (await self.rail_stations())["stations"]}
        return self._crs_index

    async def home_stations(self, h) -> list[str]:
        """The home's RAIL_STATIONS, or the two stations nearest it (within 15 km)."""
        if h.rail_stations:
            return h.rail_stations[:4]
        from .home import _km
        lat, lon = h.lat, h.lon
        near = sorted(((_km(lat, lon, s["lat"], s["lon"]), s["crs"]) for s in (await self._crs()).values()))
        return [c for d, c in near[:2] if d < 15]

    @staticmethod
    def _name_index(crs: dict[str, dict]) -> tuple[re.Pattern, dict[str, str]]:
        """One regex matching any station name (longest first) -> CRS. Short names are skipped:
        "Bow", "Ash" and the like are too easily ordinary words."""
        by_name: dict[str, str] = {}
        for s in crs.values():
            for n in {s["name"], re.sub(r"\s*\(.*?\)", "", s["name"])}:
                if len(n) >= 3 and n not in by_name:
                    by_name[n] = s["crs"]
        long_ = "|".join(re.escape(n) for n in sorted((n for n in by_name if len(n) >= 5), key=len, reverse=True))
        short = "|".join(re.escape(n) for n in by_name if len(n) < 5)
        # short names ("Rye", "Bow") only count straight after a routing word
        return re.compile(rf"(?<![\w'])({long_})(?![\w'])|(?:between|and|at|to|from)\s+({short})(?![\w'])"), by_name

    @staticmethod
    def _stations_in(text: str, index: tuple[re.Pattern, dict[str, str]]) -> list[str]:
        rx, by_name = index
        seen: list[str] = []
        for m in rx.finditer(text):
            c = by_name[m.group(1) or m.group(2)]
            if c not in seen:
                seen.append(c)
        return seen[:10]

    # ------------------------------------------------------------ departures
    async def rail_board(self, crs: str) -> dict:
        crs = (crs or "").upper()
        if not CRS_RE.match(crs):
            return {"error": "bad station code"}
        if not self.cfg.rail_ldb_key:
            return {"error": "no_key", "note": "Add RAIL_LDB_KEY for live departures"}

        async def fetch() -> dict:
            d = await self._json(LDB_URL.format(crs=crs), params={"numRows": str(BOARD_ROWS)},
                                 headers={"x-apikey": self.cfg.rail_ldb_key, "User-Agent": "UKSitRep"},
                                 timeout=RAIL_TIMEOUT)
            services = []
            for s in d.get("trainServices") or []:
                calls = ((s.get("subsequentCallingPoints") or [{}])[0] or {}).get("callingPoint") or []
                dest = s.get("destination") or []
                services.append({
                    "id": s.get("serviceID"), "std": s.get("std"), "etd": s.get("etd"), "platform": s.get("platform"),
                    "operator": s.get("operator"), "dest": " & ".join(x.get("locationName", "") for x in dest),
                    "via": next((x.get("via") for x in dest if x.get("via")), None),
                    "cancelled": bool(s.get("isCancelled")) or s.get("etd") == "Cancelled",
                    "late": _late_min(s.get("std"), s.get("etd")),
                    "reason": _clean(s.get("cancelReason") or s.get("delayReason")) or None,
                    "calls": [c.get("locationName") for c in calls][:12], "coaches": s.get("length") or None,
                })
            msgs = [_clean(m.get("Value") if isinstance(m, dict) else m) for m in d.get("nrccMessages") or []]
            return {"crs": crs, "name": d.get("locationName") or crs, "generated": d.get("generatedAt"),
                    "services": services, "messages": [m for m in msgs if m],
                    "platforms": bool(d.get("platformAvailable"))}

        return await self.cache.get(f"rail:{crs}", 60, fetch)

    # ------------------------------------------------------------ disruptions
    async def rail_disruptions(self) -> dict:
        if not self.cfg.rail_disruptions_key:
            return {"error": "no_key", "note": "Add RAIL_DISRUPTIONS_KEY for National Rail disruptions"}

        async def fetch() -> dict:
            ptr = await self._json(DISRUPTIONS_URL, headers={"x-apikey": self.cfg.rail_disruptions_key,
                                                             "User-Agent": "UKSitRep"}, timeout=RAIL_TIMEOUT)
            url = ptr.get("bulletinURL") or ""
            if not url.startswith("https://") or ".amazonaws.com/" not in url:
                raise RuntimeError("unexpected bulletin location")
            items = await self._json(url, timeout=aiohttp.ClientTimeout(total=60))
            crs = await self._crs()
            names = self._name_index(crs)
            now = datetime.now(timezone.utc).isoformat()
            out = []
            for b in items if isinstance(items, list) else []:
                if b.get("category") not in DISRUPTION_KINDS or str(b.get("isCleared")).lower() == "true":
                    continue
                start, end = _local(b.get("startDateTime")), _local(b.get("expiryDateTime"))
                if (start and start > now) or (end and end < now):
                    continue
                hits = [crs[a["crsCode"]] for a in b.get("affectedStation") or [] if crs.get(a.get("crsCode") or "")]
                if not hits:  # most bulletins only name the stations: "No trains between X and Y"
                    hits = [crs[c] for c in self._stations_in(b.get("description") or "", names)]
                stations = [{"crs": s["crs"], "name": s["name"], "lat": s["lat"], "lon": s["lon"]} for s in hits]
                out.append({
                    "id": b.get("id"), "kind": b["category"], "severity": b.get("severity"),
                    "text": _clean(b.get("description"))[:400], "url": b.get("url"), "start": start, "end": end,
                    "operators": [o.get("tocName") for o in b.get("affectedOperators") or [] if isinstance(o, dict)][:4],
                    "stations": stations[:30],
                })
            out.sort(key=lambda x: (x["kind"] != "Disruption", x.get("severity") or "P9", x.get("start") or ""))
            return {"updated": now, "disruptions": out}

        return await self.cache.get("rail_disruptions", 300, fetch)
