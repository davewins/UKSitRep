"""Second batch of feeds: rivers, lightning, air quality, bathing water, ISS,
satellite fire hotspots, TfL status and street crime.

Mixed into Situation, so these use its session, cache and helpers.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from collections import deque
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import logging
import math
import re
import time
from typing import Any
import urllib.parse

import aiohttp

_LOGGER = logging.getLogger("uksitrep")

EA_FLOOD = "https://environment.data.gov.uk/flood-monitoring"
SEPA_KIWIS = "https://timeseries.sepa.org.uk/KiWIS/KiWIS"
SEPA_BASE = {"service": "kisters", "type": "queryServices", "datasource": "0", "format": "json"}

# Lightning: Blitzortung.org volunteer network (private, non-commercial use)
BLITZ_HOSTS = ("ws1", "ws7", "ws8", "ws5")
BLITZ_BOX = (45.0, -16.0, 63.0, 8.0)  # S, W, N, E: UK, Ireland and near continent
BLITZ_KEEP_S = 2 * 3600

# Air quality / pollen points (Open-Meteo CAMS), a spread across the UK
AIR_POINTS = [
    ("London", 51.507, -0.128), ("Birmingham", 52.486, -1.890), ("Manchester", 53.481, -2.242),
    ("Leeds", 53.801, -1.549), ("Newcastle", 54.978, -1.618), ("Liverpool", 53.408, -2.991),
    ("Bristol", 51.455, -2.588), ("Plymouth", 50.375, -4.143), ("Exeter", 50.718, -3.534),
    ("Torquay", 50.462, -3.525), ("Southampton", 50.910, -1.404), ("Brighton", 50.822, -0.137),
    ("Norwich", 52.630, 1.297), ("Cambridge", 52.205, 0.122), ("Oxford", 51.752, -1.258),
    ("Gloucester", 51.864, -2.244), ("Nottingham", 52.954, -1.158), ("Sheffield", 53.381, -1.470),
    ("Hull", 53.744, -0.332), ("Carlisle", 54.892, -2.934), ("Cardiff", 51.481, -3.179),
    ("Swansea", 51.621, -3.944), ("Aberystwyth", 52.415, -4.083), ("Belfast", 54.597, -5.930),
    ("Derry", 54.997, -7.309), ("Glasgow", 55.864, -4.252), ("Edinburgh", 55.953, -3.188),
    ("Aberdeen", 57.150, -2.094), ("Inverness", 57.478, -4.224), ("Dundee", 56.462, -2.971),
]
POLLEN = ("alder", "birch", "grass", "mugwort", "olive", "ragweed")

FIRMS_SOURCES = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
FIRMS_BOX = "-11,49.5,2.5,61"  # W,S,E,N

TFL_MODES = "tube,overground,dlr,elizabeth-line,tram"

FUEL_API = "https://www.fuel-finder.service.gov.uk/api/v1"
FUEL_TYPES = ("E10", "E5", "B7_STANDARD", "B7_PREMIUM", "B10", "HVO")

CRIME_MIN_PER_MONTH = 10  # fewer than this within a mile usually means the force didn't submit

PHOTO_UA = {"User-Agent": "UKSitRep/1.5 (+https://github.com/davewins/UKSitRep)"}  # Planespotters requires a contact URL
LATEST_VERSION_URL = "https://raw.githubusercontent.com/davewins/UKSitRep/main/uksitrep/__init__.py"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
HEX_RE = re.compile(r"^[0-9a-f]{6}$")
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,8}$")

ISS_TLE = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"


def _in_box(lat: float, lon: float, box: tuple[float, float, float, float]) -> bool:
    return box[0] <= lat <= box[2] and box[1] <= lon <= box[3]


def _lzw_decode(s: str) -> str:
    """Blitzortung frames are LZW-compressed JSON strings."""
    if not s:
        return s
    table: dict[int, str] = {}
    prev = cur = s[0]
    out = [cur]
    code = 256
    for ch in s[1:]:
        n = ord(ch)
        entry = ch if n < 256 else table.get(n, prev + cur)
        out.append(entry)
        cur = entry[0]
        table[code] = prev + cur
        code += 1
        prev = entry
    return "".join(out)


class LightningRelay:
    """Keeps a rolling 2 h window of strikes near the UK from Blitzortung."""

    def __init__(self, owner) -> None:
        self.owner = owner
        self.strikes: deque[tuple[float, float, float]] = deque()  # (unix s, lat, lon)
        self.connected = False
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.task or self.task.done():
            self.task = self.owner.spawn(self._run())

    async def _run(self) -> None:
        backoff, i = 5, 0
        while True:
            host = BLITZ_HOSTS[i % len(BLITZ_HOSTS)]
            i += 1
            try:
                async with self.owner.session.ws_connect(
                    f"wss://{host}.blitzortung.org/", heartbeat=60,
                    headers={"Origin": "https://map.blitzortung.org"},
                    timeout=aiohttp.ClientWSTimeout(ws_close=10),
                ) as ws:
                    await ws.send_str('{"a":111}')
                    self.connected, backoff = True, 5
                    _LOGGER.info("lightning: connected to %s.blitzortung.org", host)
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            s = json.loads(_lzw_decode(msg.data))
                            lat, lon = float(s["lat"]), float(s["lon"])
                        except (ValueError, KeyError, TypeError):
                            continue
                        if _in_box(lat, lon, BLITZ_BOX):
                            self.strikes.append((s.get("time", 0) / 1e9 or time.time(), lat, lon))
                        self._trim()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.info("lightning: %s.blitzortung.org dropped: %s", host, err)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    def _trim(self) -> None:
        cutoff = time.time() - BLITZ_KEEP_S
        while self.strikes and self.strikes[0][0] < cutoff:
            self.strikes.popleft()

    def snapshot(self) -> dict:
        self._trim()
        return {
            "updated": datetime.now(timezone.utc).isoformat(), "connected": self.connected,
            "strikes": [[round(t), round(la, 4), round(lo, 4)] for t, la, lo in self.strikes],
        }


# ------------------------------------------------------------------ ISS maths
def _gmst(dt: datetime) -> float:
    """Greenwich mean sidereal time in radians."""
    jd = dt.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t ** 3 / 38710000.0
    return math.radians(g % 360.0)


def _sun_eci(dt: datetime) -> tuple[float, float, float]:
    """Approximate Sun position (km, ECI) - plenty for day/night and eclipse tests."""
    n = dt.timestamp() / 86400.0 + 2440587.5 - 2451545.0
    lon = math.radians((280.460 + 0.9856474 * n) % 360)
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = lon + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    r = 149597870.7 * (1.00014 - 0.01671 * math.cos(g) - 0.00014 * math.cos(2 * g))
    return (r * math.cos(lam), r * math.cos(eps) * math.sin(lam), r * math.sin(eps) * math.sin(lam))


def _observer_eci(lat: float, lon: float, dt: datetime) -> tuple[float, float, float]:
    a, f = 6378.137, 1 / 298.257223563
    phi, th = math.radians(lat), _gmst(dt) + math.radians(lon)
    c = 1 / math.sqrt(1 + f * (f - 2) * math.sin(phi) ** 2)
    s = (1 - f) ** 2 * c
    return (a * c * math.cos(phi) * math.cos(th), a * c * math.cos(phi) * math.sin(th), a * s * math.sin(phi))


def _elevation(lat: float, lon: float, dt: datetime, target: tuple[float, float, float]) -> float:
    """Elevation (deg) of an ECI target as seen from lat/lon."""
    ox, oy, oz = _observer_eci(lat, lon, dt)
    rx, ry, rz = target[0] - ox, target[1] - oy, target[2] - oz
    phi, th = math.radians(lat), _gmst(dt) + math.radians(lon)
    up = (math.cos(phi) * math.cos(th), math.cos(phi) * math.sin(th), math.sin(phi))
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    return math.degrees(math.asin((rx * up[0] + ry * up[1] + rz * up[2]) / rng))


def _sunlit(sat: tuple[float, float, float], sun: tuple[float, float, float]) -> bool:
    """Cylindrical Earth-shadow test."""
    sl = math.sqrt(sum(c * c for c in sun))
    u = [c / sl for c in sun]
    along = sum(s * c for s, c in zip(sat, u))
    if along > 0:
        return True
    perp = math.sqrt(max(0.0, sum(s * s for s in sat) - along * along))
    return perp > 6371.0


def _eci_to_latlon(r: tuple[float, float, float], dt: datetime) -> tuple[float, float, float]:
    x, y, z = r
    lon = math.degrees(math.atan2(y, x) - _gmst(dt))
    lon = (lon + 540) % 360 - 180
    p = math.sqrt(x * x + y * y)
    lat = math.degrees(math.atan2(z, p))
    alt = math.sqrt(x * x + y * y + z * z) - 6371.0
    return lat, lon, alt


OSM_UK_BOX = "49.8,-8.7,60.9,1.9"   # S, W, N, E
OSM_REFRESH_S = 7 * 86400


def parse_osm_atms(els: list[dict]) -> list[dict]:
    """Overpass elements tagged amenity=atm -> small flat records. `fee` stays None when the tag
    is missing: most UK machines aren't tagged, so unknown must not be shown as free."""
    out = []
    for el in els:
        t = el.get("tags") or {}
        lat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        lon = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        operator = t.get("operator") or t.get("brand") or t.get("network")
        fee = t.get("fee") if t.get("fee") in ("no", "some", "yes") else None
        out.append({
            "id": f"{el.get('type', 'node')[0]}{el.get('id')}", "lat": round(lat, 5), "lon": round(lon, 5),
            "name": t.get("name") or operator or "Cash machine", "operator": operator,
            "fee": fee, "charge": (t.get("charge") or "")[:30] or None,
            "cash_in": t.get("cash_in") == "yes" or None,
            "hours": (t.get("opening_hours") or "")[:60] or None,
            "indoor": t.get("indoor") == "yes" or None,
            "wheelchair": t.get("wheelchair") if t.get("wheelchair") in ("yes", "limited", "no") else None,
            "url": f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}",
        })
    return out


def parse_osm_postboxes(els: list[dict]) -> list[dict]:
    """Overpass elements tagged amenity=post_box -> small records, empty fields left out (there are
    ~90,000 of them). `times` is OSM's collection_times text as mapped; it may be missing or out of date."""
    out = []
    for el in els:
        t = el.get("tags") or {}
        lat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        lon = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        typ = el.get("type", "node")
        rec = {"id": f"{typ[0]}{el.get('id')}", "lat": round(lat, 5), "lon": round(lon, 5)}
        for key, val in (("ref", t.get("ref")), ("operator", t.get("operator")), ("cypher", t.get("royal_cypher")),
                         ("type", t.get("post_box:type")), ("times", t.get("collection_times"))):
            if val:
                rec[key] = val[:80]
        out.append(rec)
    return out




def _osm_base(el: dict) -> dict | None:
    """id, position and survey date of an Overpass element (None when it has no position)."""
    t = el.get("tags") or {}
    lat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
    lon = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None
    rec = {"id": f"{el.get('type', 'node')[0]}{el.get('id')}", "lat": round(lat, 5), "lon": round(lon, 5)}
    day = t.get("check_date") or t.get("survey:date") or t.get("check_date:defibrillator")
    if day:
        rec["check"] = day[:10]
    return rec


def _osm_fill(rec: dict, t: dict, fields: tuple[tuple[str, str], ...], n: int = 100) -> dict:
    """Copy the tags named in `fields` ((record key, OSM tag)) that have a value; empty ones are left out."""
    for key, tag in fields:
        if t.get(tag):
            rec[key] = t[tag][:n]
    return rec


def parse_osm_defibs(els: list[dict]) -> list[dict]:
    """emergency=defibrillator elements. The cabinet code tag is deliberately not kept."""
    out = []
    for el in els:
        rec = _osm_base(el)
        if rec:
            t = el.get("tags") or {}
            _osm_fill(rec, t, (("name", "name"), ("loc", "defibrillator:location"), ("desc", "description"),
                               ("access", "access"), ("hours", "opening_hours"), ("operator", "operator"),
                               ("indoor", "indoor"), ("cabinet", "defibrillator:cabinet")))
            out.append(rec)
    return out


def parse_osm_watersafety(els: list[dict]) -> list[dict]:
    """emergency=life_ring and emergency=lifeguard elements -> one list, `type` says which."""
    out = []
    for el in els:
        rec = _osm_base(el)
        if rec:
            t = el.get("tags") or {}
            rec["type"] = "lifeguard" if t.get("emergency") == "lifeguard" else "life_ring"
            _osm_fill(rec, t, (("name", "name"), ("desc", "description"), ("operator", "operator"),
                               ("hours", "opening_hours"), ("seasonal", "seasonal")))
            out.append(rec)
    return out


def parse_osm_toilets(els: list[dict]) -> list[dict]:
    """amenity=toilets elements. `fee` stays absent when untagged: unknown, not free."""
    out = []
    for el in els:
        rec = _osm_base(el)
        if rec:
            t = el.get("tags") or {}
            _osm_fill(rec, t, (("name", "name"), ("fee", "fee"), ("charge", "charge"), ("hours", "opening_hours"),
                               ("wheelchair", "wheelchair"), ("access", "access"), ("operator", "operator"),
                               ("changing", "changing_table"), ("desc", "description")))
            out.append(rec)
    return out


# name -> Overpass tags (any of), saved file, record parser, biggest box one request may cover (degrees lat, lon), markers per request
OSM_POINTS = {
    "atms": (("amenity=atm",), "osm_atms.json", parse_osm_atms, (0.1, 0.2), 500),
    "postboxes": (("amenity=post_box",), "osm_postboxes.json", parse_osm_postboxes, (0.05, 0.1), 500),
    "defibs": (("emergency=defibrillator",), "osm_defibs.json", parse_osm_defibs, (0.15, 0.3), 500),
    "watersafety": (("emergency=life_ring", "emergency=lifeguard"), "osm_watersafety.json", parse_osm_watersafety, (0.25, 0.5), 500),
    "toilets": (("amenity=toilets",), "osm_toilets.json", parse_osm_toilets, (0.15, 0.3), 500),
}

class MoreFeeds:
    """Mixin for Situation."""

    _osm_pts: dict[str, dict] | None = None   # per OSM_POINTS name: records sorted by lat, their lats, fetched, retry_at, task

    # ------------------------------------------------------------ river levels
    async def rivers(self) -> dict:
        async def stations() -> dict:
            store = self.cfg.data_dir / "river_stations.json"

            async def fetch() -> dict:
                try:
                    d = await self._json(f"{EA_FLOOD}/id/stations", params={
                        "parameter": "level", "_view": "full", "_limit": "10000"},
                        timeout=aiohttp.ClientTimeout(total=90))
                    out = {}
                    for s in d.get("items", []):
                        sc = s.get("stageScale")
                        if not isinstance(sc, dict) or not isinstance(s.get("lat"), (int, float)):
                            continue
                        measures = s.get("measures") or []
                        if isinstance(measures, dict):
                            measures = [measures]
                        # prefer the main stage gauge over downstream / tidal measures
                        m = next((m for m in measures if m.get("qualifier") == "Stage"), None)
                        if not m:
                            continue
                        hr = sc.get("highestRecent")
                        out[m["@id"].rsplit("/", 1)[-1]] = {
                            "id": s.get("notation"), "name": s.get("label"), "river": s.get("riverName"),
                            "town": s.get("town"), "lat": s["lat"], "lon": s["long"], "rloi": s.get("RLOIid"),
                            "low": sc.get("typicalRangeLow"), "high": sc.get("typicalRangeHigh"),
                            "record": hr.get("value") if isinstance(hr, dict) else None,
                        }
                    if not out:
                        raise RuntimeError("no stations")
                    await asyncio.to_thread(lambda: store.write_text(json.dumps(out)))
                    return out
                except Exception as err:  # noqa: BLE001
                    try:
                        return await asyncio.to_thread(lambda: json.loads(store.read_text()))
                    except (OSError, ValueError):
                        raise RuntimeError(f"EA stations: {err}") from err

            return await self.cache.get("river_stations", 86400, fetch)

        async def fetch() -> dict:
            st = await stations()
            if isinstance(st, dict) and "error" in st and len(st) == 1:
                raise RuntimeError(st["error"])
            d = await self._json(f"{EA_FLOOD}/data/readings", params={
                "latest": "", "parameter": "level", "_limit": "10000"})
            now = datetime.now(timezone.utc)
            out = []
            for r in d.get("items", []):
                mid = str(r.get("measure", "")).rsplit("/", 1)[-1]
                s = st.get(mid)
                v = r.get("value")
                if not s or not isinstance(v, (int, float)):
                    continue
                try:
                    age = (now - datetime.fromisoformat(r["dateTime"].replace("Z", "+00:00"))).total_seconds()
                except (KeyError, ValueError):
                    continue
                if age > 6 * 3600:  # gauge offline
                    continue
                prev = self.river_prev.get(mid)
                trend = 0
                if prev and prev[0] != r["dateTime"]:
                    trend = 1 if v - prev[1] > 0.01 else -1 if prev[1] - v > 0.01 else 0
                elif prev:
                    trend = prev[2]
                self.river_prev[mid] = (r["dateTime"], v, trend)
                state = "normal"
                # some gauges' "highest recent" is on another datum; only trust it above the typical range
                rec = s["record"] if s["record"] is not None and s["high"] is not None and s["record"] > s["high"] else None
                if rec is not None and v >= rec:
                    state = "record"
                elif s["high"] is not None and v > s["high"]:
                    state = "high"
                elif s["low"] is not None and v < s["low"]:
                    state = "low"
                out.append({**s, "level": round(v, 3), "time": r["dateTime"], "trend": trend, "state": state})
            try:
                out.extend(await self._sepa_gauges())
            except Exception as err:  # noqa: BLE001 - England still useful without Scotland
                _LOGGER.warning("SEPA river levels failed: %s", err)
            return {"updated": now.isoformat(), "gauges": out}

        return await self.cache.get("rivers", 600, fetch)

    async def _sepa(self, **params) -> list[list]:
        return await self._json(SEPA_KIWIS, params={**SEPA_BASE, **params}, timeout=aiohttp.ClientTimeout(total=60))

    async def _sepa_gauges(self) -> list[dict]:
        """Scottish gauges (SEPA KiWIS). 'Typical range' comes from SEPA's long-term
        monthly min/max for the current month; SEPA also publishes rising/falling."""
        async def meta() -> dict:
            async def fetch() -> dict:
                rows = await self._sepa(request="getTimeseriesList", stationparameter_name="Level",
                                        returnfields="station_no,station_name,station_latitude,station_longitude,ts_id,ts_name")
                head, rows = rows[0], [dict(zip(rows[0], r)) for r in rows[1:]]
                rivers = await self._sepa(request="getStationList", returnfields="station_no,river_name")
                river = {r[0]: r[1] for r in rivers[1:]}
                st: dict[str, dict] = {}
                for r in rows:
                    s_ = st.setdefault(r["station_no"], {"name": r["station_name"], "river": river.get(r["station_no"]),
                                                         "lat": r["station_latitude"], "lon": r["station_longitude"]})
                    s_[r["ts_name"]] = r["ts_id"]
                st = {k: v for k, v in st.items() if "15minute" in v and v["lat"] and v["lon"]}
                # this month's long-term max/min (the series hold one value per calendar month)
                now = datetime.now(timezone.utc)
                frm = now.strftime("%Y-%m-01")
                lt_ids = [v[n] for v in st.values() for n in ("LongTermValue.Month.Max", "LongTermValue.Month.Min") if n in v]
                lt: dict[str, float] = {}
                for i in range(0, len(lt_ids), 300):
                    vals = await self._sepa(request="getTimeseriesValues", ts_id=",".join(lt_ids[i:i + 300]),
                                            **{"from": frm, "to": frm + "T23:59:59", "returnfields": "Timestamp,Value"})
                    for v in vals:
                        if v.get("data"):
                            lt[str(v["ts_id"])] = v["data"][0][1]
                for v in st.values():
                    v["high"] = lt.get(v.get("LongTermValue.Month.Max"))
                    v["low"] = lt.get(v.get("LongTermValue.Month.Min"))
                return st
            return await self.cache.get("sepa_meta", 86400, fetch)

        st = await meta()
        if "error" in st and len(st) == 1:
            raise RuntimeError(st["error"])
        level_ids = {v["15minute"]: k for k, v in st.items()}
        trend_ids = {v["15m.RisingFalling.Cmd"]: k for k, v in st.items() if "15m.RisingFalling.Cmd" in v}
        ids, vals = [*level_ids, *trend_ids], []
        for i in range(0, len(ids), 350):  # KiWIS rejects very long id lists
            vals += await self._sepa(request="getTimeseriesValueLayer", ts_id=",".join(ids[i:i + 350]),
                                     returnfields="ts_id,timestamp,ts_value")
        now = datetime.now(timezone.utc)
        level: dict[str, tuple[str, float]] = {}
        trend: dict[str, int] = {}
        for v in vals:
            tid, val = str(v.get("ts_id")), v.get("ts_value")
            if val is None:
                continue
            if tid in level_ids:
                level[level_ids[tid]] = (v["timestamp"], float(val))
            elif tid in trend_ids:
                trend[trend_ids[tid]] = int(round(float(val)))
        out = []
        for no, (ts, val) in level.items():
            try:
                if (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() > 6 * 3600:
                    continue
            except ValueError:
                continue
            s_ = st[no]
            high, low = s_.get("high"), s_.get("low")
            state = "high" if high is not None and val > high else "low" if low is not None and val < low else "normal"
            out.append({
                "id": no, "name": s_["name"], "river": s_.get("river"), "town": None,
                "lat": float(s_["lat"]), "lon": float(s_["lon"]), "rloi": None,
                "low": round(low, 3) if low is not None else None, "high": round(high, 3) if high is not None else None,
                "record": None, "level": round(val, 3), "time": ts.replace(".000Z", "Z"), "trend": trend.get(no, 0),
                "state": state, "source": "SEPA", "url": f"https://waterlevels.sepa.org.uk/Station/{no}",
            })
        return out

    # ------------------------------------------------------------ lightning
    async def lightning(self) -> dict:
        if not self.cfg.enable_lightning:
            return {"error": "disabled", "strikes": []}
        self.lightning_relay.start()
        return self.lightning_relay.snapshot()

    # ------------------------------------------------------------ air quality
    async def air_quality(self) -> dict:
        async def fetch() -> dict:
            pollen = ",".join(f"{p}_pollen" for p in POLLEN)
            d = await self._json("https://air-quality-api.open-meteo.com/v1/air-quality", params={
                "latitude": ",".join(str(p[1]) for p in AIR_POINTS),
                "longitude": ",".join(str(p[2]) for p in AIR_POINTS),
                "current": f"european_aqi,pm2_5,pm10,nitrogen_dioxide,ozone,{pollen}",
                "timezone": "Europe/London",
            })
            if isinstance(d, dict):
                d = [d]
            out = []
            for (name, lat, lon), loc in zip(AIR_POINTS, d):
                c = loc.get("current", {})
                out.append({
                    "name": name, "lat": lat, "lon": lon, "aqi": c.get("european_aqi"),
                    "pm25": c.get("pm2_5"), "pm10": c.get("pm10"), "no2": c.get("nitrogen_dioxide"),
                    "o3": c.get("ozone"), "pollen": {p: c.get(f"{p}_pollen") for p in POLLEN},
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "points": out}

        return await self.cache.get("air_quality", 3600, fetch)

    # ------------------------------------------------------------ bathing water
    async def bathing(self) -> dict:
        def name(o: Any) -> str | None:
            if isinstance(o, dict):
                n = o.get("name") or o.get("label")
                if isinstance(n, list):
                    n = n[0] if n else None
                return n.get("_value") if isinstance(n, dict) else n
            return None

        async def fetch() -> dict:
            d = await self._json("https://environment.data.gov.uk/doc/bathing-water.json",
                                 params={"_pageSize": "1000"}, timeout=aiohttp.ClientTimeout(total=60))
            now = datetime.now(timezone.utc)
            out = []
            for b in d.get("result", {}).get("items", []):
                sp = b.get("samplingPoint") or {}
                if not isinstance(sp.get("lat"), (int, float)):
                    continue
                risk = b.get("latestRiskPrediction")
                risk_level = None
                if isinstance(risk, dict):
                    exp = (risk.get("expiresAt") or {}).get("_value")
                    try:
                        live = exp and datetime.fromisoformat(exp).replace(tzinfo=timezone.utc) > now
                    except ValueError:
                        live = False
                    risk_level = name(risk.get("riskLevel")) if live else None
                bid = b.get("eubwidNotation")
                sample = b.get("latestSampleAssessment")
                out.append({
                    "id": bid, "name": name(b), "lat": sp["lat"], "lon": sp["long"],
                    "classification": name((b.get("latestComplianceAssessment") or {}).get("complianceClassification")),
                    "risk": risk_level, "heavy_rain_risk": bool(b.get("waterQualityImpactedByHeavyRain")),
                    "sample_date": sample.split("/date/")[1][:8] if isinstance(sample, str) and "/date/" in sample else None,
                    "url": f"https://environment.data.gov.uk/bwq/profiles/profile.html?site={bid}",
                })
            return {"updated": now.isoformat(), "sites": out}

        return await self.cache.get("bathing", 6 * 3600, fetch)

    # ------------------------------------------------------------ ISS
    async def iss(self, lat: float | None = None, lon: float | None = None) -> dict:
        from sgp4.api import Satrec, jday  # imported lazily: only this feed needs it

        async def tle() -> dict:
            async def fetch() -> dict:
                lines = [ln.strip() for ln in (await self._text(ISS_TLE)).splitlines() if ln.strip()]
                l1 = next(ln for ln in lines if ln.startswith("1 "))
                l2 = next(ln for ln in lines if ln.startswith("2 "))
                return {"l1": l1, "l2": l2}
            return await self.cache.get("iss_tle", 6 * 3600, fetch)

        t = await tle()
        if "error" in t:
            return {"error": t["error"]}
        sat = Satrec.twoline2rv(t["l1"], t["l2"])

        def pos(dt: datetime) -> tuple[float, float, float] | None:
            jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
            err, r, _ = sat.sgp4(jd, fr)
            return None if err else tuple(r)

        now = datetime.now(timezone.utc).replace(microsecond=0)
        r = pos(now)
        if r is None:
            return {"error": "propagation failed"}
        la, lo, alt = _eci_to_latlon(r, now)
        track = []
        for m in range(-45, 96, 3):  # roughly one orbit behind, one ahead
            dt = now + timedelta(minutes=m)
            p = pos(dt)
            if p:
                tla, tlo, _ = _eci_to_latlon(p, dt)
                track.append([round(tla, 3), round(tlo, 3), m])
        out = {"updated": now.isoformat(), "lat": round(la, 4), "lon": round(lo, 4), "alt_km": round(alt),
               "sunlit": _sunlit(r, _sun_eci(now)), "track": track}
        if lat is not None and lon is not None:
            key = f"iss_passes:{lat:.1f},{lon:.1f}"
            out["passes"] = await self.cache.get(key, 1800, lambda: asyncio.to_thread(
                self._iss_passes, pos, lat, lon, now))
            out["observer"] = [round(lat, 1), round(lon, 1)]
        return out

    @staticmethod
    def _iss_passes(pos, lat: float, lon: float, start: datetime) -> list[dict]:
        """Visible passes over the next 3 days: ISS above 10 deg, sunlit, observer in darkness."""
        passes: list[dict] = []
        cur: dict | None = None
        for i in range(0, 3 * 86400, 20):
            dt = start + timedelta(seconds=i)
            r = pos(dt)
            if r is None:
                continue
            el = _elevation(lat, lon, dt, r)
            if el >= 10:
                sun = _sun_eci(dt)
                visible = _sunlit(r, sun) and _elevation(lat, lon, dt, sun) < -6
                if cur is None:
                    cur = {"start": dt, "max_el": el, "max_time": dt, "visible_from": dt if visible else None,
                           "visible_to": dt if visible else None}
                if el > cur["max_el"]:
                    cur["max_el"], cur["max_time"] = el, dt
                if visible:
                    cur["visible_from"] = cur["visible_from"] or dt
                    cur["visible_to"] = dt
                cur["end"] = dt
            elif cur is not None:
                if cur["visible_from"]:
                    passes.append({
                        "start": cur["visible_from"].isoformat(), "end": cur["visible_to"].isoformat(),
                        "max_el": round(cur["max_el"]), "max_time": cur["max_time"].isoformat(),
                    })
                cur = None
                if len(passes) >= 5:
                    break
        return passes

    # ------------------------------------------------------------ aircraft photo + route
    async def aircraft_info(self, hexid: str, callsign: str) -> dict:
        """Photo (Planespotters, else adsbdb/airport-data) and scheduled route (adsbdb) for one aircraft."""
        hexid, callsign = hexid.strip().lower(), callsign.strip().upper()
        if not HEX_RE.match(hexid):
            return {"error": "bad hex"}

        async def photo() -> dict:
            async def fetch() -> dict:
                try:
                    d = await self._json(f"https://api.planespotters.net/pub/photos/hex/{hexid}", headers=PHOTO_UA)
                    p = (d.get("photos") or [None])[0]
                    if p:
                        return {"src": (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src"),
                                "link": p.get("link"), "credit": f"© {p.get('photographer') or 'unknown'} / Planespotters.net"}
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("planespotters %s: %s", hexid, err)
                try:
                    d = await self._json(f"https://api.adsbdb.com/v0/aircraft/{hexid}", headers=PHOTO_UA)
                    a = (d.get("response") or {}).get("aircraft") or {}
                    if a.get("url_photo_thumbnail"):
                        return {"src": a["url_photo_thumbnail"], "link": a.get("url_photo"), "credit": "airport-data.com",
                                "owner": a.get("registered_owner")}
                    return {"owner": a.get("registered_owner")} if a else {}
                except Exception:  # noqa: BLE001 - adsbdb answers 404 for unknown airframes
                    return {}
            return await self.cache.get(f"acphoto:{hexid}", 86400, fetch)

        async def route() -> dict:
            if not CALLSIGN_RE.match(callsign):
                return {}

            async def fetch() -> dict:
                try:
                    d = await self._json(f"https://api.adsbdb.com/v0/callsign/{callsign}", headers=PHOTO_UA)
                except Exception:  # noqa: BLE001 - unknown callsign
                    return {}
                r = (d.get("response") or {}).get("flightroute") or {}
                def ap(x: dict | None) -> dict | None:
                    return {"name": x.get("name"), "iata": x.get("iata_code"), "icao": x.get("icao_code"),
                            "city": x.get("municipality"), "country": x.get("country_name")} if x else None
                return {"airline": (r.get("airline") or {}).get("name"), "origin": ap(r.get("origin")),
                        "destination": ap(r.get("destination"))} if r else {}
            return await self.cache.get(f"acroute:{callsign}", 6 * 3600, fetch)

        p, r = await asyncio.gather(photo(), route())
        return {"photo": p, "route": r}

    # ------------------------------------------------------------ version check
    async def version_info(self, running: str) -> dict:
        """This server's version and the latest on GitHub main, so the page can say when to redeploy."""
        async def fetch() -> dict:
            async with self.session.get(LATEST_VERSION_URL, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                m = re.search(r'__version__ = "([\d.]+)"', await r.text())
            return {"latest": m.group(1)} if m else {}

        latest = (await self.cache.get("latest_version", 6 * 3600, fetch)).get("latest")
        vt = lambda v: tuple(int(x) for x in v.split("."))
        return {"version": running, "latest": latest, "newer": bool(latest and vt(latest) > vt(running))}

    # ------------------------------------------------------------ vessel photo
    async def vessel_info(self, mmsi: str, imo: str, name: str) -> dict:
        """A photo of one ship from Wikimedia: Wikidata's image for the IMO or MMSI number, else a photo
        from the ship's Commons category (Commons files ships under "IMO 1234567", which keeps working
        through renames). Small craft without an IMO number rarely have one."""
        mmsi, imo = mmsi.strip(), imo.strip()
        if not re.fullmatch(r"\d{9}", mmsi) or (imo and not re.fullmatch(r"\d{7}", imo)):
            return {"error": "bad mmsi or imo"}
        name = re.sub(r"[^\w ]", "", name or "").strip().lower()

        async def commons_file(title: str) -> dict | None:
            d = await self._json(COMMONS_API, params={
                "action": "query", "titles": title, "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "320",
                "iiextmetadatafilter": "Artist|LicenseShortName", "format": "json"}, headers=PHOTO_UA)
            for page in (d.get("query") or {}).get("pages", {}).values():
                ii = (page.get("imageinfo") or [None])[0]
                if ii and ii.get("thumburl"):
                    meta = ii.get("extmetadata") or {}
                    artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()
                    lic = (meta.get("LicenseShortName") or {}).get("value", "")
                    return {"src": ii["thumburl"].split("?")[0], "link": ii.get("descriptionurl"),
                            "credit": " · ".join(x for x in (f"© {artist[:60]}" if artist else "", lic, "Wikimedia Commons") if x)}
            return None

        async def fetch() -> dict:
            # 1. Wikidata: the ship's item and its chosen image
            where = " UNION ".join(([f'{{ ?s wdt:P458 "{imo}" }}'] if imo else []) + [f'{{ ?s wdt:P587 "{mmsi}" }}'])
            try:
                d = await self._json(WIKIDATA_SPARQL, params={"query": f"SELECT ?img WHERE {{ {where} ?s wdt:P18 ?img }} LIMIT 1",
                                                              "format": "json"}, headers=PHOTO_UA)
                rows = d.get("results", {}).get("bindings", [])
            except Exception as err:  # noqa: BLE001 - the query service is slow at times; Commons alone still works
                _LOGGER.debug("wikidata %s: %s", imo or mmsi, err)
                rows = []
            if rows:
                f = await commons_file("File:" + urllib.parse.unquote(rows[0]["img"]["value"].rsplit("/", 1)[-1]))
                if f:
                    return f
            if not imo:
                return {}
            # 2. Commons: "Category:IMO n" holds the ship's category (one per name it has had) or its files
            d = await self._json(COMMONS_API, params={"action": "query", "list": "categorymembers", "cmtitle": f"Category:IMO {imo}",
                                                      "cmtype": "subcat|file", "cmlimit": "20", "format": "json"}, headers=PHOTO_UA)
            members = [m["title"] for m in (d.get("query") or {}).get("categorymembers", [])]
            files = [m for m in members if m.startswith("File:")]
            cats = sorted((m for m in members if m.startswith("Category:")), key=lambda c: name not in c.lower())  # its current name first
            if not files and cats:
                d = await self._json(COMMONS_API, params={"action": "query", "list": "categorymembers", "cmtitle": cats[0],
                                                          "cmtype": "file", "cmlimit": "20", "format": "json"}, headers=PHOTO_UA)
                files = [m["title"] for m in (d.get("query") or {}).get("categorymembers", [])]
            files = [f for f in files if re.search(r"\.(jpe?g|png|webp)$", f, re.I)]
            return (await commons_file(files[0]) or {}) if files else {}

        return {"photo": await self.cache.get(f"vsphoto:{imo or mmsi}", 7 * 86400, fetch)}

    # ------------------------------------------------------------ satellite fire hotspots
    async def fires(self) -> dict:
        if not self.cfg.firms_key:
            return {"error": "no_key", "fires": []}

        async def fetch() -> dict:
            async def one(src: str) -> list[dict]:
                txt = await self._text(
                    f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{self.cfg.firms_key}/{src}/{FIRMS_BOX}/2")
                if not txt.startswith("latitude"):
                    raise RuntimeError(txt.strip()[:120])
                rows = []
                for row in csv.DictReader(io.StringIO(txt)):
                    try:
                        hhmm = row["acq_time"].zfill(4)
                        when = datetime.strptime(f"{row['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
                        rows.append({
                            "lat": float(row["latitude"]), "lon": float(row["longitude"]),
                            "time": when.isoformat(), "frp": float(row.get("frp") or 0),
                            "confidence": row.get("confidence"), "sat": row.get("satellite"),
                            "night": row.get("daynight") == "N",
                        })
                    except (KeyError, ValueError):
                        continue
                return rows

            results = await asyncio.gather(*(one(s) for s in FIRMS_SOURCES), return_exceptions=True)
            fires, errors = [], []
            for src, res in zip(FIRMS_SOURCES, results):
                if isinstance(res, Exception):
                    errors.append(f"{src}: {res}")
                else:
                    fires.extend(res)
            if not fires and errors:
                raise RuntimeError("; ".join(errors))
            return {"updated": datetime.now(timezone.utc).isoformat(), "fires": fires, "errors": errors}

        return await self.cache.get("fires", 1800, fetch)

    # ------------------------------------------------------------ TfL
    async def tfl(self) -> dict:
        async def fetch() -> dict:
            d = await self._json(f"https://api.tfl.gov.uk/Line/Mode/{TFL_MODES}/Status")
            lines = []
            for ln in d:
                sts = ln.get("lineStatuses") or []
                worst = min(sts, key=lambda s: s.get("statusSeverity", 10)) if sts else {}
                lines.append({
                    "name": ln.get("name"), "mode": ln.get("modeName"),
                    "severity": worst.get("statusSeverity", 10),
                    "status": worst.get("statusSeverityDescription", "Good Service"),
                    "reason": " ".join(dict.fromkeys(s.get("reason", "").strip() for s in sts if s.get("reason"))),
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "lines": lines}

        return await self.cache.get("tfl", 300, fetch)

    # ------------------------------------------------------------ fuel prices (GOV.UK Fuel Finder)
    async def _fuel_token(self) -> str:
        tok = self.fuel_token
        if tok and time.time() < tok[1] - 120:
            return tok[0]
        async with self.session.post(f"{FUEL_API}/oauth/generate_access_token", json={
                "client_id": self.cfg.fuel_client_id, "client_secret": self.cfg.fuel_client_secret},
                timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            d = await r.json(content_type=None)
        d = d.get("data", d)
        self.fuel_token = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
        return d["access_token"]

    async def _fuel_batches(self, path: str) -> list[dict]:
        """All batches (500 each) of a Fuel Finder list, one request at a time as the API requires."""
        out: list[dict] = []
        async with self.fuel_lock:
            for batch in range(1, 80):
                headers = {"Authorization": f"Bearer {await self._fuel_token()}", "Accept": "application/json"}
                async with self.session.get(f"{FUEL_API}/{path}", params={"batch-number": str(batch)}, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 404:  # past the last batch
                        break
                    if r.status == 429:
                        await asyncio.sleep(5)
                        continue
                    r.raise_for_status()
                    d = await r.json(content_type=None)
                items = d if isinstance(d, list) else d.get("data", [])
                out.extend(items)
                if len(items) < 500:
                    break
                await asyncio.sleep(0.7)  # stay well under 100 requests a minute
        return out

    async def _osm_points(self, name: str, bbox: tuple[float, float, float, float] | None) -> dict:
        """Points of one kind from OpenStreetMap inside the viewer's box. The whole UK list is fetched from
        Overpass once a week in the background and kept in memory, so viewers never wait on Overpass.
        OSM has no live status: these are mapped positions and tags."""
        _tags, fname, _parse, span, cap = OSM_POINTS[name]
        if not bbox:
            return {"error": "bbox required"}
        s, w, n, e = bbox
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 180) or n - s > span[0] or e - w > span[1]:
            return {"error": "zoom in", name: []}
        if self._osm_pts is None:
            self._osm_pts = {}
        st = self._osm_pts.setdefault(name, {"recs": None, "lats": [], "fetched": 0.0, "retry_at": 0.0, "task": None})
        store = self.cfg.data_dir / fname
        if st["recs"] is None:
            try:
                saved = await asyncio.to_thread(lambda: json.loads(store.read_text()))
                self._osm_set(st, saved[name], float(saved.get("fetched", 0)))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if time.time() - st["fetched"] > OSM_REFRESH_S and time.time() > st["retry_at"] \
                and (not st["task"] or st["task"].done()):
            st["task"] = self.spawn(self._refresh_osm_points(name, st, store))
        if st["recs"] is None:
            return {"error": "loading: the list is downloading, try again in a few minutes"}
        hits = [r for r in st["recs"][bisect_left(st["lats"], s):bisect_right(st["lats"], n)] if w <= r["lon"] <= e]
        return {"updated": datetime.fromtimestamp(st["fetched"], timezone.utc).isoformat(),
                name: hits[:cap], "truncated": len(hits) > cap}

    @staticmethod
    def _osm_set(st: dict, recs: list[dict], fetched: float) -> None:
        recs.sort(key=lambda r: r["lat"])
        st["recs"], st["lats"], st["fetched"] = recs, [r["lat"] for r in recs], fetched

    async def _refresh_osm_points(self, name: str, st: dict, store) -> None:
        from .feeds import OVERPASS_MIRRORS, UA
        tags, _f, parse, _span, _cap = OSM_POINTS[name]
        parts = "".join(f'nwr["{k}"="{v}"]({OSM_UK_BOX});' for k, v in (t.split("=") for t in tags))
        query = f'[out:json][timeout:400];({parts});out center tags;'
        for mirror in OVERPASS_MIRRORS:
            try:
                async with self.session.post(mirror, data={"data": query}, headers=UA,
                                             timeout=aiohttp.ClientTimeout(total=480)) as r:
                    r.raise_for_status()
                    els = (await r.json(content_type=None)).get("elements", [])
            except Exception as err:  # noqa: BLE001 - try the next mirror
                _LOGGER.info("overpass %s failed: %s", mirror, err)
                continue
            recs = parse(els)
            del els
            if not recs:
                continue
            now = time.time()
            await asyncio.to_thread(lambda: store.write_text(json.dumps({"fetched": now, name: recs}, separators=(",", ":"))))
            self._osm_set(st, recs, now)
            _LOGGER.info("OSM %s refreshed: %d", name, len(recs))
            return
        st["retry_at"] = time.time() + 1800
        _LOGGER.warning("OSM %s: all Overpass mirrors failed, retrying in 30 min", name)

    async def fuel(self, bbox: tuple[float, float, float, float] | None = None) -> dict:
        """Forecourt prices. The full list is ~2.5 MB, so viewers ask for their bounding box (S,W,N,E)."""
        if not (self.cfg.fuel_client_id and self.cfg.fuel_client_secret):
            return {"error": "no_key", "stations": []}
        d = await self._fuel_all()
        if "error" in d:
            return d
        if bbox is None:
            return {**d, "stations": []}
        s, w, n, e = bbox
        return {**d, "stations": [o for o in d["stations"] if s <= o["lat"] <= n and w <= o["lon"] <= e][:1500]}

    async def _fuel_all(self) -> dict:
        async def stations() -> dict:
            store = self.cfg.data_dir / "fuel_stations.json"

            async def fetch() -> dict:
                try:
                    out = {}
                    for s in await self._fuel_batches("pfs"):
                        loc = s.get("location") or {}
                        if s.get("permanent_closure") or not isinstance(loc.get("latitude"), (int, float)):
                            continue
                        out[s["node_id"]] = {
                            "name": (s.get("trading_name") or "").title(), "brand": (s.get("brand_name") or "").title(),
                            "lat": loc["latitude"], "lon": loc["longitude"], "postcode": loc.get("postcode"),
                            "town": (loc.get("city") or "").title(), "closed": bool(s.get("temporary_closure")),
                            "motorway": bool(s.get("is_motorway_service_station")),
                            "supermarket": bool(s.get("is_supermarket_service_station")),
                        }
                    if not out:
                        raise RuntimeError("no forecourts returned")
                    await asyncio.to_thread(lambda: store.write_text(json.dumps(out)))
                    return out
                except Exception as err:  # noqa: BLE001
                    try:
                        return await asyncio.to_thread(lambda: json.loads(store.read_text()))
                    except (OSError, ValueError):
                        raise RuntimeError(f"Fuel Finder forecourts: {err}") from err

            return await self.cache.get("fuel_stations", 86400, fetch)

        async def fetch() -> dict:
            st = await stations()
            if "error" in st and len(st) == 1:
                raise RuntimeError(st["error"])
            out = []
            for p in await self._fuel_batches("pfs/fuel-prices"):
                s = st.get(p.get("node_id"))
                if not s:
                    continue
                prices, latest = {}, None
                for fp in p.get("fuel_prices") or []:
                    v = fp.get("price")
                    if fp.get("fuel_type") in FUEL_TYPES and isinstance(v, (int, float)) and 80 <= v <= 350:
                        prices[fp["fuel_type"]] = round(v, 1)
                        latest = max(latest or "", fp.get("price_last_updated") or "")
                if prices:
                    out.append({**s, "prices": prices, "updated": latest or None})
            if not out:
                raise RuntimeError("no prices returned")
            avg = {}
            for ft in FUEL_TYPES:
                vals = sorted(o["prices"][ft] for o in out if ft in o["prices"] and not o["closed"])
                if vals:
                    avg[ft] = {"median": vals[len(vals) // 2], "n": len(vals)}
            return {"updated": datetime.now(timezone.utc).isoformat(), "stations": out, "national": avg}

        return await self.cache.get("fuel", 1800, fetch)

    # ------------------------------------------------------------ street crime
    async def crime(self, lat: float, lon: float) -> dict:
        """police.uk street-level crime within a mile of a point.

        Uses the latest published month. Some forces stop submitting for a while
        (Gloucestershire since early 2026), so if the latest month has next to
        nothing we step back to the most recent month that has real data.
        """
        if not (49.8 <= lat <= 61 and -8.7 <= lon <= 2):
            return {"error": "outside the UK"}
        lat, lon = round(lat, 2), round(lon, 2)

        async def month_of(date: str | None) -> list[dict]:
            params = {"lat": str(lat), "lng": str(lon)}
            if date:
                params["date"] = date
            for attempt in range(3):
                try:
                    return await self._json("https://data.police.uk/api/crimes-street/all-crime",
                                            params=params, timeout=aiohttp.ClientTimeout(total=30))
                except aiohttp.ClientResponseError as err:
                    if err.status != 429 or attempt == 2:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))  # police.uk allows short bursts only
            return []

        async def fetch() -> dict:
            latest_list = await self.cache.get("police_dates", 86400, lambda: self._json(
                "https://data.police.uk/api/crimes-street-dates"))
            latest = latest_list[0]["date"] if isinstance(latest_list, list) and latest_list else None
            crimes = await month_of(None)
            month = latest
            if len(crimes) < CRIME_MIN_PER_MONTH and latest:
                y, m = map(int, latest.split("-"))
                for _ in range(12):
                    y, m = (y, m - 1) if m > 1 else (y - 1, 12)
                    older = await month_of(f"{y:04d}-{m:02d}")
                    if len(older) >= CRIME_MIN_PER_MONTH:
                        crimes, month = older, f"{y:04d}-{m:02d}"
                        break
                    await asyncio.sleep(0.4)
            spots: dict[str, dict] = {}
            for c in crimes:
                loc = c.get("location") or {}
                try:
                    la, lo = float(loc["latitude"]), float(loc["longitude"])
                except (KeyError, TypeError, ValueError):
                    continue
                month = c.get("month") or month
                k = f"{la:.5f},{lo:.5f}"
                spot = spots.setdefault(k, {"lat": la, "lon": lo, "street": (loc.get("street") or {}).get("name"),
                                            "crimes": {}})
                cat = c.get("category", "other")
                spot["crimes"][cat] = spot["crimes"].get(cat, 0) + 1
            return {"updated": datetime.now(timezone.utc).isoformat(), "month": month, "latest": latest,
                    "older": bool(latest and month and month != latest),
                    "centre": [lat, lon], "spots": list(spots.values())}

        return await self.cache.get(f"crime:{lat},{lon}", 12 * 3600, fetch)
