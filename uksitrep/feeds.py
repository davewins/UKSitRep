"""Feed fetchers for UK SitRep.

Fetches, caches and normalises public UK feeds into small JSON documents
served by :mod:`uksitrep.server`.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import aiohttp
from .config import Config
from .fire_services import FireServices
from .home import HomeArea
from .more_feeds import LightningRelay, MoreFeeds
from .bus import Bus
from .bustimes import BusTimes
from .energy import Energy
from .rail import Rail
from .sky import SkyWatch
from .hems_sites import HEMS_EXCLUDE_KM, HEMS_SITES
from .places import PLACE_ORDER, PLACES

_LOGGER = logging.getLogger("uksitrep")

UA = {"User-Agent": "UKSitRep/1.0 (+https://github.com/)"}
TIMEOUT = aiohttp.ClientTimeout(total=20)

# UK + surrounding waters
BBOX = (48.5, -12.0, 61.5, 4.0)  # S, W, N, E

# adsb.lol radius queries (max 250 nm) that between them cover the UK
ADSB_POINTS = [(51.4, -1.8), (54.4, -3.6), (57.9, -4.0)]

# Callsign prefix / registration heuristics for special aircraft
AIRCRAFT_RULES = [
    ("police", re.compile(r"^(UKP|NPAS|POL)")),
    ("hems", re.compile(r"^(HLE|HELIMED|MEDIC)")),
    ("sar", re.compile(r"^(SRD|RESCUE|CGRES|COASTG)")),
]
SAR_REG = re.compile(r"^G-(MCG|CGU)")
EMERGENCY_SQUAWKS = {"7500": "Hijack", "7600": "Radio failure", "7700": "Emergency"}

MET_REGIONS = {
    "os": ("Orkney & Shetland", 59.6, -2.0),
    "he": ("Highlands & Eilean Siar", 57.6, -5.2),
    "gr": ("Grampian", 57.2, -2.7),
    "st": ("Strathclyde", 55.8, -4.9),
    "ta": ("Central, Tayside & Fife", 56.5, -3.6),
    "dg": ("SW Scotland, Lothian & Borders", 55.4, -3.3),
    "ni": ("Northern Ireland", 54.65, -6.75),
    "wl": ("Wales", 52.3, -3.7),
    "nw": ("North West England", 54.0, -2.8),
    "ne": ("North East England", 54.9, -1.8),
    "yh": ("Yorkshire & Humber", 53.9, -1.2),
    "wm": ("West Midlands", 52.5, -2.2),
    "em": ("East Midlands", 52.9, -0.9),
    "ee": ("East of England", 52.4, 0.6),
    "sw": ("South West England", 50.9, -3.5),
    "se": ("London & South East", 51.3, -0.3),
}

BBC_FEEDS = {
    "UK": "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "England": "https://feeds.bbci.co.uk/news/england/rss.xml",
    "Scotland": "https://feeds.bbci.co.uk/news/scotland/rss.xml",
    "Wales": "https://feeds.bbci.co.uk/news/wales/rss.xml",
    "N Ireland": "https://feeds.bbci.co.uk/news/northern_ireland/rss.xml",
}
NEWS_HIGH = re.compile(
    r"\b(killed|dead|death|dies|died|murder|stabb\w*|shoot\w*|shot|explosion|blast|"
    r"terror\w*|evacuat\w*|major incident|crash\w*|collapse\w*|missing|manhunt)\b",
    re.I,
)
NEWS_MED = re.compile(
    r"\b(fire|blaze|flood\w*|storm|arrest\w*|police|injur\w*|closed|closure|"
    r"disruption|cancel\w*|warning|outage|power cut|strike)\b",
    re.I,
)

CITY_WEATHER = [
    ("London", 51.507, -0.128), ("Birmingham", 52.486, -1.890),
    ("Manchester", 53.480, -2.242), ("Newcastle", 54.978, -1.618),
    ("Glasgow", 55.864, -4.252), ("Edinburgh", 55.953, -3.188),
    ("Aberdeen", 57.150, -2.094), ("Inverness", 57.478, -4.224),
    ("Lerwick", 60.155, -1.145), ("Belfast", 54.597, -5.930),
    ("Cardiff", 51.481, -3.179), ("Plymouth", 50.376, -4.143),
    ("Norwich", 52.630, 1.297), ("Southampton", 50.910, -1.404),
    ("Stornoway", 58.209, -6.388), ("Tewkesbury", 51.992, -2.160),
]

# Sea-state sample points, named after the nearest Shipping Forecast area
SEA_POINTS = [
    ("Fair Isle", 59.6, -3.6), ("Viking", 59.5, 1.5), ("Cromarty", 57.9, -2.4),
    ("Forties", 57.3, 0.8), ("Forth", 56.3, -1.2), ("Tyne", 55.2, -0.9),
    ("Dogger", 54.6, 2.0), ("Humber", 53.6, 1.2), ("Thames", 51.9, 1.9),
    ("Dover", 50.95, 1.2), ("Wight", 50.4, -1.0), ("Portland", 50.2, -2.6),
    ("Plymouth", 49.9, -4.6), ("Sole", 49.6, -7.5), ("Lundy", 51.4, -5.0),
    ("Fastnet", 51.0, -8.0), ("Irish Sea", 53.7, -5.0), ("Shannon", 52.8, -10.8),
    ("Malin", 55.7, -7.3), ("Hebrides", 57.6, -8.6), ("Minch", 58.0, -5.8),
    ("Bristol Channel", 51.35, -3.9),
]

UK_TZ = ZoneInfo("Europe/London")
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
PKG_DATA = Path(__file__).with_name("data")
# Live storm overflow (sewage) status. English companies publish the Water UK "Stream"
# schema (Status 1 = discharging); Dwr Cymru and Scottish Water have their own layouts.
STREAM_OVERFLOWS = {
    "South West Water": "https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services/NEH_outlets_PROD/FeatureServer/0",
    "Wessex Water": "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Storm_Overflow_Activity/FeatureServer/0",
    "Southern Water": "https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services/SouthernWater_StormOverflowActivity_PROD_view/FeatureServer/0",
    "Thames Water": "https://services2.arcgis.com/g6o32ZDQ33GpCIu3/arcgis/rest/services/Thames_Water_Storm_Overflow_Activity_(Production)_view/FeatureServer/0",
    "Severn Trent": "https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services/Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0",
    "Anglian Water": "https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services/stream_service_outfall_locations_view/FeatureServer/0",
    "United Utilities": "https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services/United_Utilities_Storm_Overflow_Activity/FeatureServer/0",
    "Yorkshire Water": "https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0",
    "Northumbrian Water": "https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services/Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0",
}
DCWW_OVERFLOWS = "https://services3.arcgis.com/KLNF7YxtENPLYVey/arcgis/rest/services/Spill_Prod/FeatureServer/0"
SCOTTISH_OVERFLOWS = "https://services3.arcgis.com/Bb8lfThdhugyc4G3/arcgis/rest/services/Scottish_Water_Storm_Overflow_Activity/FeatureServer/0"
SEWAGE_RECENT_H = 48  # SAS keeps an alert live for 48 h after a discharge stops

# Admiralty EasyTide (UKHO): keyless, ~700 UK & Irish ports, 7-day predictions
EASYTIDE = "https://easytide.admiralty.co.uk/Home/"
TIDE_STATION_ID = re.compile(r"^\d{4}[A-Z]?$")


def parse_osm_webcams(els: list[dict]) -> list[dict]:
    """Overpass elements tagged contact:webcam -> map cameras."""
    out = []
    for e in els:
        t = e.get("tags", {})
        link = t.get("contact:webcam", "").split(";")[0].strip()
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        if not link.startswith("http") or lat is None:
            continue
        is_img = bool(re.search(r"\.(jpe?g|png|gif)(\?|$)", link, re.I))
        out.append({
            "id": f"osm{e['type'][0]}{e['id']}",
            "name": t.get("name") or t.get("description") or t.get("operator") or "Webcam",
            "lat": lat, "lon": lon, "image": link if is_img else None, "video": None,
            "url": link, "source": "OpenStreetMap",
        })
    return out


def _km(a: float, b: float, c: float, d: float) -> float:
    x = (math.sin(math.radians(c - a) / 2) ** 2
         + math.cos(math.radians(a)) * math.cos(math.radians(c)) * math.sin(math.radians(d - b) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(x))


def _near_hems_site(lat: float, lon: float) -> str | None:
    for name, la, lo in HEMS_SITES:
        if abs(la - lat) < 0.1 and abs(lo - lon) < 0.15 and _km(lat, lon, la, lo) < HEMS_EXCLUDE_KM:
            return name
    return None


def _in_bbox(lat: Any, lon: Any) -> bool:
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and BBOX[0] <= lat <= BBOX[2]
        and BBOX[1] <= lon <= BBOX[3]
    )


def _rfc822(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return None


class Cache:
    """TTL cache with per-key locking so concurrent viewers share one fetch."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, key: str, ttl: float, fn: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            try:
                value = await fn()
            except Exception as err:  # noqa: BLE001 - keep serving stale data
                _LOGGER.warning("uk_situation feed %s failed: %s", key, err)
                if hit:
                    return hit[1]
                # remember the failure briefly so a dead upstream isn't retried (and waited on) every call
                failed = {"error": str(err) or type(err).__name__}
                self._data[key] = (time.monotonic() - ttl + min(ttl, 120), failed)
                return failed
            self._data[key] = (time.monotonic(), value)
            return value


def _rnli_callouts(launches: list[dict]) -> list[dict]:
    """One entry per call-out: a station's launches within 30 min of each other are one shout
    (the feed lists each boat, sometimes twice). Keeps the first launch's id and time."""
    out: list[dict] = []
    for l in sorted(launches, key=lambda l: l.get("time") or ""):
        prev = next((o for o in reversed(out) if o["station"] == l["station"]), None)
        if prev and prev["time"] and l["time"] and \
                (datetime.fromisoformat(l["time"]) - datetime.fromisoformat(prev["last"])).total_seconds() <= 1800:
            if l.get("lifeboat") and l["lifeboat"] not in prev["boats"]:
                prev["boats"].append(l["lifeboat"])
            prev["last"] = l["time"]
            continue
        out.append({**l, "boats": [l["lifeboat"]] if l.get("lifeboat") else [], "last": l["time"]})
    for o in out:
        o["lifeboat"] = ", ".join(o["boats"]) or None
    return sorted(out, key=lambda l: l.get("time") or "", reverse=True)


class Situation(MoreFeeds, FireServices, HomeArea, SkyWatch, Rail, Bus, BusTimes, Energy):
    """All feed fetchers plus long-lived state (aircraft trails, AIS)."""

    def __init__(self, cfg: Config, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.nh_keys = cfg.nh_keys
        self.session = session
        self.tasks: set[asyncio.Task] = set()
        self.cache = Cache()
        self.trails: dict[str, deque] = {}
        self.trail_seen: dict[str, float] = {}
        self.flood_area_pos: dict[str, tuple[float, float] | None] = {}
        self.hems_last: dict[str, dict] = {}
        self.hems_events: list[dict] = []
        self.adsb_complete = False
        self.adsb_backoff = 0.0
        self.adsb_retry_at = 0.0
        self.last_aircraft: dict | None = None
        self.rnli_stations: dict[str, list[float]] = {}
        self.place_cache: dict[str, dict | None] = {}
        self.worldcam_task: asyncio.Task | None = None
        self.osm_task: asyncio.Task | None = None
        self.osm_retry_at = 0.0
        self.obs_task: asyncio.Task | None = None
        self.obs_retry_at = 0.0
        self.rail_task: asyncio.Task | None = None
        self.rail_retry_at = 0.0
        self._crs_index: dict | None = None
        self.noc_task: asyncio.Task | None = None
        self.noc_retry_at = 0.0
        self.bt_task: asyncio.Task | None = None
        self.bt_retry_at = 0.0
        self._noc: tuple[float, dict] | None = None
        self.ais = AisRelay(self, cfg.ais_key) if cfg.ais_key else None
        self.lightning_relay = LightningRelay(self)
        self.river_prev: dict[str, tuple[str, float, int]] = {}
        self.fuel_token: tuple[str, float] | None = None
        self.fuel_lock = asyncio.Lock()

    def spawn(self, coro) -> asyncio.Task:
        """Start a background task and keep a reference so it is not GC'd."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ---------------------------------------------------------------- helpers
    async def _json(self, url: str, **kw) -> Any:
        timeout = kw.pop("timeout", TIMEOUT)
        headers = kw.pop("headers", UA)
        async with self.session.get(url, headers=headers, timeout=timeout, **kw) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def _text(self, url: str) -> str:
        async with self.session.get(url, headers=UA, timeout=TIMEOUT) as r:
            r.raise_for_status()
            return await r.text()

    # --------------------------------------------------------------- aviation
    async def aircraft(self) -> dict:
        async def fetch() -> dict:
            urls = [f"https://api.adsb.lol/v2/point/{la}/{lo}/250" for la, lo in ADSB_POINTS]
            urls.append("https://api.adsb.lol/v2/mil")
            if time.monotonic() < self.adsb_retry_at:  # rate-limited: keep showing the last picture
                if self.last_aircraft:
                    return {**self.last_aircraft, "stale": True}
                return {"error": "adsb.lol rate limit, backing off", "aircraft": [], "hems_events": self.hems_events}
            # one at a time: adsb.lol limits bursts per IP
            results: list[Any] = []
            for u in urls:
                try:
                    results.append(await self._json(u))
                except Exception as err:  # noqa: BLE001
                    results.append(err)
                    if getattr(err, "status", None) == 429:
                        retry = getattr(err, "headers", None) or {}
                        try:
                            wait = float(retry.get("Retry-After", 0))
                        except (TypeError, ValueError):
                            wait = 0
                        self.adsb_backoff = min(300.0, max(30.0, self.adsb_backoff * 2, wait))
                        self.adsb_retry_at = time.monotonic() + self.adsb_backoff
                        _LOGGER.warning("adsb.lol rate limit (429); pausing %.0f s", self.adsb_backoff)
                        break
            merged: dict[str, dict] = {}
            ok = 0
            for res in results:
                if isinstance(res, Exception):
                    _LOGGER.debug("adsb fetch failed: %s", res)
                    continue
                ok += 1
                for ac in res.get("ac", []):
                    if not _in_bbox(ac.get("lat"), ac.get("lon")):
                        continue
                    merged.setdefault(ac["hex"], ac)
            if not ok:
                err = next(r for r in results if isinstance(r, Exception))
                raise RuntimeError(f"all adsb.lol requests failed ({type(err).__name__}: {err})")
            self.adsb_complete = ok == len(urls)
            if self.adsb_complete:
                self.adsb_backoff = 0.0
            self.last_aircraft = self._normalise_aircraft(merged)
            return self.last_aircraft

        return await self.cache.get("aircraft", 12, fetch)

    def _normalise_aircraft(self, merged: dict[str, dict]) -> dict:
        now = time.monotonic()
        out = []
        for hexid, ac in merged.items():
            flight = (ac.get("flight") or "").strip()
            reg = (ac.get("r") or "").strip()
            alt = ac.get("alt_baro")
            squawk = ac.get("squawk")
            cls = "civil"
            if ac.get("dbFlags", 0) & 1:
                cls = "military"
            for name, rx in AIRCRAFT_RULES:
                if rx.match(flight.upper()):
                    cls = name
            if SAR_REG.match(reg):
                cls = "sar"
            emerg = EMERGENCY_SQUAWKS.get(squawk)
            if not emerg and ac.get("emergency") not in (None, "none"):
                emerg = ac["emergency"]

            trail = self.trails.setdefault(hexid, deque(maxlen=90))
            point = [round(ac["lat"], 4), round(ac["lon"], 4)]
            if not trail or trail[-1][:2] != point:
                trail.append(point + [alt if isinstance(alt, int) else 0])
            self.trail_seen[hexid] = now

            out.append({
                "hex": hexid,
                "flight": flight,
                "reg": reg,
                "type": ac.get("t"),
                "lat": ac["lat"],
                "lon": ac["lon"],
                "alt": alt,
                "gs": ac.get("gs"),
                "track": ac.get("track", ac.get("true_heading", ac.get("calc_track"))),
                "vrate": ac.get("baro_rate", ac.get("geom_rate")),
                "squawk": squawk,
                "cat": ac.get("category"),
                "cls": cls,
                "emergency": emerg,
                "trail": list(trail),
            })
        self._detect_hems_scenes(out)
        # forget aircraft we have not seen for 5 minutes
        for hexid in [h for h, t in self.trail_seen.items() if now - t > 300]:
            self.trail_seen.pop(hexid, None)
            self.trails.pop(hexid, None)
        return {"updated": datetime.now(timezone.utc).isoformat(), "aircraft": out,
                "hems_events": self.hems_events}

    def _hems_event(self, a: dict, kind: str) -> None:
        now = datetime.now(timezone.utc)
        for ev in self.hems_events:
            if ev["hex"] == a["hex"] and _km(ev["lat"], ev["lon"], a["lat"], a["lon"]) < 2:
                ev["last"] = now.isoformat()
                if kind == "landed":
                    ev["kind"] = "landed"
                return
        self.hems_events.append({
            "hex": a["hex"], "flight": a["flight"] or a["reg"], "reg": a["reg"], "cls": a["cls"],
            "lat": a["lat"], "lon": a["lon"], "kind": kind,
            "first": now.isoformat(), "last": now.isoformat(),
        })

    def _detect_hems_scenes(self, aircraft: list[dict]) -> None:
        """Flag air ambulance / SAR helicopters landing away from bases and hospitals."""
        seen = set()
        for a in aircraft:
            if a["cls"] not in ("hems", "sar"):
                continue
            seen.add(a["hex"])
            alt, gs = a["alt"], a["gs"]
            low = alt == "ground" or (isinstance(alt, (int, float)) and alt <= 600)
            if low and (gs is None or gs < 40) and not _near_hems_site(a["lat"], a["lon"]):
                self._hems_event(a, "landed")
            self.hems_last[a["hex"]] = a
        # dropped off radar while low = probably landed somewhere coverage can't see
        for hexid in [h for h in self.hems_last if h not in seen]:
            a = self.hems_last.pop(hexid)
            if not self.adsb_complete:
                continue  # a partial poll would look like everything vanished
            alt = a["alt"]
            low = alt == "ground" or (isinstance(alt, (int, float)) and alt <= 1500)
            if low and (a["vrate"] or 0) <= 0 and not _near_hems_site(a["lat"], a["lon"]):
                self._hems_event(a, "lost_low")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.hems_events = [e for e in self.hems_events if e["last"] >= cutoff]

    # ---------------------------------------------------------------- webcams
    async def webcams(self) -> dict:
        async def fetch() -> dict:
            cams: list[dict] = []
            errors = []
            try:
                tfl = await self._json("https://api.tfl.gov.uk/Place/Type/JamCam")
                for c in tfl:
                    props = {p["key"]: p["value"] for p in c.get("additionalProperties", [])}
                    if props.get("available") == "false":
                        continue
                    cams.append({
                        "id": c.get("id"), "name": c.get("commonName"), "lat": c.get("lat"), "lon": c.get("lon"),
                        "image": props.get("imageUrl"), "video": props.get("videoUrl"),
                        "url": props.get("videoUrl") or props.get("imageUrl"), "source": "TfL JamCam",
                    })
            except Exception as err:  # noqa: BLE001
                errors.append(f"TfL: {err}")
            cams.extend(await self._osm_cams())
            if self.cfg.enable_worldcam:
                cams.extend(await self._worldcams())
            cams.extend(self.cfg.curated_cams())
            if not cams:
                raise RuntimeError("; ".join(errors) or "no webcams")
            if errors:  # don't keep a partial list for a whole day
                asyncio.get_running_loop().call_later(1800, self.cache._data.pop, "webcams", None)
            return {"updated": datetime.now(timezone.utc).isoformat(), "cams": cams, "errors": errors}

        return await self.cache.get("webcams", 6 * 3600, fetch)

    async def _osm_cams(self) -> list[dict]:
        """OSM contact:webcam tags: served from a saved copy, refreshed daily in the background.

        Overpass can take minutes or fail outright, so viewers never wait on it. A copy
        bundled with the image covers a fresh /data volume until the first refresh lands.
        """
        store = self.cfg.data_dir / "osm_webcams.json"
        saved: dict = {"fetched": 0, "cams": []}
        for path in (store, PKG_DATA / "osm_webcams.json"):
            try:
                raw = await asyncio.to_thread(lambda: json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
            # the bundled seed (and pre-1.1 saves) are bare lists: refresh as soon as possible
            saved = {"fetched": 0, "cams": raw} if isinstance(raw, list) else raw
            break
        due = time.time() - saved.get("fetched", 0) > 86400 and time.time() > self.osm_retry_at
        if due and (not self.osm_task or self.osm_task.done()):
            self.osm_task = self.spawn(self._refresh_osm(store))
        return saved.get("cams", [])

    async def _refresh_osm(self, store: Path) -> None:
        query = '[out:json][timeout:120];nwr["contact:webcam"](49.8,-8.7,60.9,1.9);out center tags;'
        for mirror in OVERPASS_MIRRORS:
            try:
                async with self.session.post(
                    mirror, data={"data": query}, headers=UA, timeout=aiohttp.ClientTimeout(total=150),
                ) as r:
                    r.raise_for_status()
                    els = (await r.json(content_type=None)).get("elements", [])
            except Exception as err:  # noqa: BLE001
                _LOGGER.info("overpass %s failed: %s", mirror, err)
                continue
            cams = parse_osm_webcams(els)
            if not cams:
                continue
            await asyncio.to_thread(lambda: store.write_text(json.dumps({"fetched": time.time(), "cams": cams})))
            self.cache._data.pop("webcams", None)  # pick up the new list on the next request
            _LOGGER.info("OSM webcams refreshed: %d cameras", len(cams))
            return
        self.osm_retry_at = time.time() + 1800
        _LOGGER.warning("OSM webcams: all Overpass mirrors failed, retrying in 30 min")

    # ---------------------------------------------------------------- sewage
    async def sewage(self) -> dict:
        """Storm overflows discharging now, or that stopped within the last 48 h."""
        async def query(url: str, where: str) -> list[dict]:
            params = {"where": where, "outFields": "*", "outSR": "4326", "returnGeometry": "true",
                      "resultRecordCount": "2000", "f": "json"}
            d = await self._json(url + "/query", params=params)
            if "error" in d:
                raise RuntimeError(d["error"].get("message", "query failed"))
            return [f for f in d.get("features", []) if f.get("geometry")]

        def ms(v: Any) -> str | None:
            return datetime.fromtimestamp(v / 1000, timezone.utc).isoformat() if isinstance(v, (int, float)) else None

        def local(v: Any) -> str | None:  # Dwr Cymru gives UK local time without a zone
            if not v:
                return None
            try:
                return datetime.fromisoformat(v[:19]).replace(tzinfo=UK_TZ).astimezone(timezone.utc).isoformat()
            except ValueError:
                return None

        async def stream(company: str, url: str, since: str) -> list[dict]:
            out = []
            for f in await query(url, f"Status=1 OR LatestEventEnd > TIMESTAMP '{since}'"):
                a = {k.lower(): v for k, v in f["attributes"].items()}
                active = a.get("status") == 1
                out.append({
                    "id": a.get("id"), "company": company, "name": f"Storm overflow {a.get('id') or ''}".strip(),
                    "water": a.get("receivingwatercourse"), "lat": f["geometry"]["y"], "lon": f["geometry"]["x"],
                    "active": active, "start": ms(a.get("latesteventstart")),
                    "end": None if active else ms(a.get("latesteventend")),
                })
            return out

        async def wales() -> list[dict]:
            out = []
            where = "status IN ('Overflow Operating','Overflow Not Operating (Has in the last 24 hours)')"
            for f in await query(DCWW_OVERFLOWS, where):
                a = f["attributes"]
                active = a.get("status") == "Overflow Operating"
                out.append({
                    "id": a.get("DCWW_ID") or a.get("permit_number"), "company": "Dŵr Cymru Welsh Water",
                    "name": a.get("asset_name"), "water": a.get("Receiving_Water"),
                    "bathing": a.get("Linked_Bathing_Water"), "lat": f["geometry"]["y"], "lon": f["geometry"]["x"],
                    "active": active, "start": local(a.get("start_date_time_discharge")),
                    "end": None if active else local(a.get("stop_date_time_discharge")),
                })
            return out

        async def scotland() -> list[dict]:
            out = []
            for f in await query(SCOTTISH_OVERFLOWS, "STATUS_NSOH=1 OR STATUS_DESCRIPTION LIKE 'RO%'"):
                a = f["attributes"]
                active = a.get("STATUS_NSOH") == 1
                out.append({
                    "id": a.get("ASSET_ID"), "company": "Scottish Water", "name": (a.get("ASSET_NAME") or "").title(),
                    "water": a.get("RECEIVING_WATER"), "lat": f["geometry"]["y"], "lon": f["geometry"]["x"],
                    "active": active, "start": a.get("START_DATETIME"), "end": None if active else a.get("END_DATETIME"),
                })
            return out

        async def fetch() -> dict:
            since = (datetime.now(timezone.utc) - timedelta(hours=SEWAGE_RECENT_H)).strftime("%Y-%m-%d %H:%M:%S")
            jobs = {c: stream(c, u, since) for c, u in STREAM_OVERFLOWS.items()}
            jobs["Dŵr Cymru Welsh Water"] = wales()
            jobs["Scottish Water"] = scotland()
            results = await asyncio.gather(*jobs.values(), return_exceptions=True)
            overflows, errors = [], []
            for company, res in zip(jobs, results):
                if isinstance(res, Exception):
                    errors.append(f"{company}: {res}")
                else:
                    overflows.extend(res)
            if not overflows and errors:
                raise RuntimeError("; ".join(errors))
            return {"updated": datetime.now(timezone.utc).isoformat(), "overflows": overflows,
                    "active": sum(o["active"] for o in overflows), "errors": errors}

        return await self.cache.get("sewage", 600, fetch)

    # ---------------------------------------------------------------- tides
    async def tide_stations(self) -> dict:
        async def fetch() -> dict:
            store = self.cfg.data_dir / "tide_stations.json"
            try:
                d = await self._json(EASYTIDE + "GetStations")
                stations = []
                for f in d.get("features", []):
                    p, (lon, lat) = f["properties"], f["geometry"]["coordinates"][:2]
                    name = p.get("Name", "")
                    stations.append({
                        "id": p["Id"], "name": name.title() if name.isupper() else name,
                        "country": p.get("Country"), "lat": lat, "lon": lon,
                    })
                if not stations:
                    raise RuntimeError("empty station list")
                await asyncio.to_thread(lambda: store.write_text(json.dumps(stations)))
            except Exception as err:  # noqa: BLE001
                try:
                    stations = await asyncio.to_thread(lambda: json.loads(store.read_text()))
                except (OSError, ValueError):
                    raise RuntimeError(f"EasyTide stations: {err}") from err
            return {"updated": datetime.now(timezone.utc).isoformat(), "stations": stations}

        return await self.cache.get("tide_stations", 7 * 86400, fetch)

    async def tides(self, station: str) -> dict:
        """High/low water for one port, plus a height curve for the popup chart."""
        if not TIDE_STATION_ID.match(station):
            return {"error": "bad station id"}
        known = {s["id"]: s for s in (await self.tide_stations()).get("stations", [])}
        if station not in known:
            return {"error": "unknown station"}

        def utc(t: str) -> str:  # EasyTide gives UTC without a zone, with ragged fractions
            return t[:19] + "Z"

        async def fetch() -> dict:
            d = await self._json(EASYTIDE + "GetPredictionData", params={"stationId": station})
            now = datetime.now(timezone.utc)
            events = [
                {"type": "HW" if e.get("eventType") == 0 else "LW", "time": utc(e["dateTime"]),
                 "height": round(e["height"], 2) if e.get("height") is not None else None}
                for e in d.get("tidalEventList", []) if e.get("dateTime")
            ]
            lo, hi = (now - timedelta(hours=12)).isoformat()[:19], (now + timedelta(hours=48)).isoformat()[:19]
            curve = [
                [utc(h["dateTime"]), round(h["height"], 2)]
                for h in d.get("tidalHeightOccurrenceList", []) if lo <= h["dateTime"][:19] <= hi
            ]
            return {"station": known[station], "updated": now.isoformat(), "events": events, "curve": curve}

        return await self.cache.get(f"tide:{station}", 6 * 3600, fetch)

    async def _worldcams(self) -> list[dict]:
        """WorldCam.eu directory: served from a saved copy, refreshed weekly in the background."""
        store = self.cfg.data_dir / "worldcam.json"
        try:
            saved = await asyncio.to_thread(lambda: json.loads(store.read_text()))
        except (OSError, ValueError):
            saved = {"fetched": 0, "cams": {}}
        stale = time.time() - saved.get("fetched", 0) > 7 * 86400
        if stale and (not self.worldcam_task or self.worldcam_task.done()):
            self.worldcam_task = self.spawn(self._refresh_worldcams(store, saved))
        return [
            {"id": f"wc{cid}", "name": c["name"], "lat": c["lat"], "lon": c["lon"],
             "image": c.get("img"), "video": None, "url": c["url"], "source": "WorldCam",
             "preview": True}
            for cid, c in saved.get("cams", {}).items() if c.get("name")
        ]

    async def _refresh_worldcams(self, store: Path, saved: dict) -> None:
        hdr = {**UA, "X-Requested-With": "XMLHttpRequest"}
        z = 7

        def tile_x(lon: float) -> int:
            return int((lon + 180) / 360 * 2 ** z)

        def tile_y(lat: float) -> int:
            r = math.radians(lat)
            return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * 2 ** z)

        S_, W_, N_, E_ = 49.8, -8.7, 60.9, 1.9
        points: dict[str, tuple[float, float]] = {}
        for x in range(tile_x(W_), tile_x(E_) + 1):
            for y in range(tile_y(N_), tile_y(S_) + 1):
                try:
                    async with self.session.get(
                        f"https://worldcam.eu/static/osm/tiles/world/{z}/{x}/{y}.json",
                        headers=hdr, timeout=TIMEOUT,
                    ) as r:
                        if r.status != 200:
                            continue
                        d = await r.json(content_type=None)
                except Exception:  # noqa: BLE001
                    continue
                for cid, lat, lon in zip(d.get("id", []), d.get("lat", []), d.get("lon", [])):
                    la, lo = lat / 1e5, lon / 1e5
                    if S_ <= la <= N_ and W_ <= lo <= E_:
                        points[str(cid)] = (la, lo)
        if not points:
            return
        known = saved.get("cams", {})
        sem = asyncio.Semaphore(3)

        async def detail(cid: str) -> None:
            async with sem:
                try:
                    async with self.session.get(
                        f"https://worldcam.eu/api/map/webcam/{cid}", headers=hdr, timeout=TIMEOUT
                    ) as r:
                        body = await r.text()
                except Exception:  # noqa: BLE001
                    return
                name = re.search(r"<strong>(.*?)</strong>", body, re.S)
                href = re.search(r'href="(/webcams/[^"]+)"', body)
                img = re.search(r'<img src="([^"]+)"', body)
                if name and href:
                    known[cid] = {
                        "name": html.unescape(name.group(1)).strip(),
                        "url": "https://worldcam.eu" + href.group(1),
                        "img": img.group(1) if img else None,
                    }
                await asyncio.sleep(0.3)

        await asyncio.gather(*(detail(c) for c in points if c not in known))
        cams = {}
        for cid, (la, lo) in points.items():
            if cid in known:
                cams[cid] = {**known[cid], "lat": la, "lon": lo}
        data = {"fetched": time.time(), "cams": cams}
        await asyncio.to_thread(lambda: store.write_text(json.dumps(data)))
        self.cache._data.pop("webcams", None)  # pick up the new list on next request
        _LOGGER.info("uk_situation: WorldCam directory refreshed, %d cameras", len(cams))

    # ------------------------------------------------------- emergency services
    async def rnli(self) -> dict:
        async def fetch() -> dict:
            if not self.rnli_stations:
                path = PKG_DATA / "rnli_stations.json"
                self.rnli_stations = await asyncio.to_thread(
                    lambda: json.loads(path.read_text())
                )
            data = await self._json("https://services.rnli.org/api/launches")
            out = []
            for l in data:
                key = _norm_station(l.get("shortName") or "")
                pos = self.rnli_stations.get(key) or next(
                    (v for k, v in self.rnli_stations.items() if key and (k.startswith(key) or key in k.split())),
                    None,
                )
                try:
                    when = datetime.fromisoformat(l["launchDate"]).replace(tzinfo=timezone.utc).isoformat()
                except (KeyError, ValueError):
                    when = None
                out.append({
                    "id": l.get("id"), "station": (l.get("shortName") or "").strip(),
                    "title": l.get("title"), "lifeboat": l.get("lifeboat_IdNo"),
                    "time": when, "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
                    "link": "https://" + l["website"] if l.get("website") else None,
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "launches": _rnli_callouts(out)}

        return await self.cache.get("rnli", 180, fetch)

    async def _place(self, name: str) -> dict | None:
        """Geocode a UK place name with postcodes.io (OS Open Names), cached."""
        if name in self.place_cache:
            return self.place_cache[name]
        res = None
        try:
            d = await self._json("https://api.postcodes.io/places", params={"q": name, "limit": 5})
            cands = d.get("result") or []
            # prefer candidates in Devon, then any
            cands.sort(key=lambda c: 0 if c.get("county_unitary") in ("Devon", "Torbay") else 1)
            if cands:
                c = cands[0]
                res = {"lat": c["latitude"], "lon": c["longitude"],
                       "district": c.get("district_borough") or c.get("county_unitary")}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("geocode %s failed: %s", name, err)
            return None  # don't cache transient failures
        self.place_cache[name] = res
        return res

    async def fire(self) -> dict:
        """Devon & Somerset (station-filtered) plus the other services in fire_services.py."""
        dsfrs, others = await asyncio.gather(self._fire_dsfrs(), self.fire_services())
        services = {**({"dsfrs": "Devon & Somerset FRS"} if self.cfg.dsfrs_stations else {}), **others["services"]}
        errors = list(others["errors"])
        if "error" in dsfrs:
            errors.append(f"Devon & Somerset FRS: {dsfrs['error']}")
        incidents = [{**i, "svc": "dsfrs"} for i in dsfrs.get("incidents", [])] + others["incidents"]
        incidents.sort(key=lambda r: r.get("time") or "", reverse=True)
        return {"updated": datetime.now(timezone.utc).isoformat(), "incidents": incidents,
                "services": services, "errors": errors, "coverage": dsfrs.get("coverage")}

    async def _fire_dsfrs(self) -> dict:
        async def fetch() -> dict:
            if not self.cfg.dsfrs_stations:
                return {"incidents": [], "disabled": True}
            params = [("attending_station[]", sid) for sid in self.cfg.dsfrs_stations]
            rows: dict[str, dict] = {}
            for page in (0, 1):
                async with self.session.get(
                    "https://www.dsfire.gov.uk/incidents", params=params + [("page", page)],
                    headers={"User-Agent": "Mozilla/5.0 (UKSitRep)"}, timeout=TIMEOUT,
                ) as r:
                    r.raise_for_status()
                    page_html = await r.text()
                for row in _parse_dsfrs(page_html):
                    rows.setdefault(row["link"], row)
            out = []
            for row in rows.values():
                place = await self._place(row["location"]) if row["location"] else None
                first_station = (row["stations"] or "").split(",")[0].strip()
                if place:
                    if place["district"] not in self.cfg.dsfrs_areas:
                        continue  # our stations helped out elsewhere
                elif first_station not in self.cfg.dsfrs_stations.values():
                    continue
                else:
                    place = await self._place(first_station)
                if not place:
                    continue
                out.append({**row, "service": "Devon & Somerset FRS", "lat": place["lat"], "lon": place["lon"],
                            "area": place.get("district")})
            out.sort(key=lambda r: r.get("time") or "", reverse=True)
            return {"updated": datetime.now(timezone.utc).isoformat(), "incidents": out,
                    "coverage": "Devon & Somerset FRS — " + " & ".join(sorted(self.cfg.dsfrs_areas))}

        return await self.cache.get("fire", 900, fetch)

    async def adsb_loop(self) -> None:
        """Poll ADS-B continuously so on-scene detection works with no viewer."""
        while True:
            try:
                await self.aircraft()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("background adsb poll failed: %s", err)
            await asyncio.sleep(15)

    @property
    def handlers(self) -> dict:
        return {
            "aircraft": self.aircraft, "weather_warnings": self.weather_warnings, "floods": self.floods,
            "city_weather": self.city_weather, "grid": self.grid, "power_cuts": self.power_cuts,
            "space": self.space, "quakes": self.quakes, "news": self.news, "roads": self.roads,
            "meteors": self.meteors, "sea": self.sea, "wind_grid": self.wind_grid, "rnli": self.rnli,
            "fire": self.fire, "webcams": self.webcams, "tide_stations": self.tide_stations,
            "sewage": self.sewage, "rivers": self.rivers, "lightning": self.lightning,
            "air_quality": self.air_quality, "bathing": self.bathing, "fires": self.fires, "tfl": self.tfl, "fuel": self.fuel,
            "home": self.home_summary, "allsky": self.allsky, "observatories": self.observatories,
            "rail_stations": self.rail_stations, "rail_disruptions": self.rail_disruptions,
            "bus_disruptions": self.bus_disruptions, "energy_regions": self.energy_regions,
            "energy_live": self.energy_live, "generators": self.generators,
        }

    # ---------------------------------------------------------------- weather
    async def weather_warnings(self) -> dict:
        async def fetch() -> dict:
            async def region(code: str) -> list[dict]:
                name, lat, lon = MET_REGIONS[code]
                xml = await self._text(
                    f"https://www.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/{code}"
                )
                root = ET.fromstring(xml)
                items = []
                for it in root.iter("item"):
                    title = (it.findtext("title") or "").strip()
                    level = "yellow"
                    for lvl in ("red", "amber", "yellow"):
                        if title.lower().startswith(lvl):
                            level = lvl
                            break
                    items.append({
                        "region": name, "code": code, "lat": lat, "lon": lon,
                        "title": title, "level": level,
                        "description": re.sub(r"<[^>]+>", " ", it.findtext("description") or "").strip(),
                        "link": it.findtext("link"),
                        "published": _rfc822(it.findtext("pubDate")),
                    })
                return items

            res = await asyncio.gather(*(region(c) for c in MET_REGIONS), return_exceptions=True)
            warnings = [w for r in res if not isinstance(r, Exception) for w in r]
            return {"updated": datetime.now(timezone.utc).isoformat(), "warnings": warnings}

        return await self.cache.get("weather_warnings", 600, fetch)

    async def floods(self) -> dict:
        async def fetch() -> dict:
            data = await self._json(
                "https://environment.data.gov.uk/flood-monitoring/id/floods?min-severity=3"
            )
            items = data.get("items", [])
            missing = [i["floodAreaID"] for i in items if i.get("floodAreaID") not in self.flood_area_pos]

            async def area(aid: str) -> None:
                try:
                    d = await self._json(f"https://environment.data.gov.uk/flood-monitoring/id/floodAreas/{aid}")
                    it = d.get("items", {})
                    self.flood_area_pos[aid] = (it.get("lat"), it.get("long"))
                except Exception:  # noqa: BLE001
                    self.flood_area_pos[aid] = None

            # be gentle: fetch unknown area positions in small batches
            for i in range(0, len(missing), 10):
                await asyncio.gather(*(area(a) for a in missing[i:i + 10]))

            out = []
            for i in items:
                pos = self.flood_area_pos.get(i.get("floodAreaID"))
                if not pos or pos[0] is None:
                    continue
                out.append({
                    "id": i.get("floodAreaID"),
                    "lat": pos[0], "lon": pos[1],
                    "severity": i.get("severity"),
                    "level": i.get("severityLevel"),
                    "area": i.get("description"),
                    "county": (i.get("floodArea") or {}).get("county"),
                    "river": (i.get("floodArea") or {}).get("riverOrSea"),
                    "message": (i.get("message") or "")[:600],
                    "changed": i.get("timeMessageChanged"),
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "floods": out}

        return await self.cache.get("floods", 600, fetch)

    async def city_weather(self) -> dict:
        async def fetch() -> dict:
            lats = ",".join(str(c[1]) for c in CITY_WEATHER)
            lons = ",".join(str(c[2]) for c in CITY_WEATHER)
            data = await self._json(
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lats}&longitude={lons}"
                "&current=temperature_2m,weather_code,wind_speed_10m,wind_gusts_10m,"
                "wind_direction_10m,precipitation&wind_speed_unit=mph&timezone=Europe/London"
            )
            if isinstance(data, dict):
                data = [data]
            out = []
            for (name, lat, lon), d in zip(CITY_WEATHER, data):
                cur = d.get("current", {})
                out.append({
                    "name": name, "lat": lat, "lon": lon,
                    "temp": cur.get("temperature_2m"),
                    "code": cur.get("weather_code"),
                    "wind": cur.get("wind_speed_10m"),
                    "gust": cur.get("wind_gusts_10m"),
                    "dir": cur.get("wind_direction_10m"),
                    "precip": cur.get("precipitation"),
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "cities": out}

        return await self.cache.get("city_weather", 1800, fetch)

    async def wind_grid(self) -> dict:
        """Coarse wind/rain/cloud grid over the UK (~100 points, hourly)."""
        async def fetch() -> dict:
            pts = [(la, round(-10 + i * 1.5, 2)) for la in range(50, 61) for i in range(9)]
            lats = ",".join(str(p[0]) for p in pts)
            lons = ",".join(str(p[1]) for p in pts)
            data = await self._json(
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lats}&longitude={lons}"
                "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m,"
                "precipitation,cloud_cover&wind_speed_unit=mph"
            )
            out = []
            for (la, lo), d in zip(pts, data):
                c = d.get("current", {})
                if c.get("wind_speed_10m") is None:
                    continue
                out.append({
                    "lat": la, "lon": lo, "wind": c["wind_speed_10m"], "dir": c.get("wind_direction_10m"),
                    "gust": c.get("wind_gusts_10m"), "temp": c.get("temperature_2m"),
                    "precip": c.get("precipitation"), "cloud": c.get("cloud_cover"),
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "points": out}

        return await self.cache.get("wind_grid", 3600, fetch)

    async def sea(self) -> dict:
        async def fetch() -> dict:
            lats = ",".join(str(p[1]) for p in SEA_POINTS)
            lons = ",".join(str(p[2]) for p in SEA_POINTS)
            marine, wind = await asyncio.gather(
                self._json(
                    "https://marine-api.open-meteo.com/v1/marine"
                    f"?latitude={lats}&longitude={lons}"
                    "&current=wave_height,wave_direction,wave_period,swell_wave_height,"
                    "swell_wave_direction,swell_wave_period,sea_surface_temperature"
                ),
                self._json(
                    "https://api.open-meteo.com/v1/forecast"
                    f"?latitude={lats}&longitude={lons}"
                    "&current=wind_speed_10m,wind_gusts_10m,wind_direction_10m,visibility"
                    "&wind_speed_unit=kn"
                ),
                return_exceptions=True,
            )
            if isinstance(marine, Exception):
                raise marine
            if isinstance(wind, Exception):
                wind = [{} for _ in SEA_POINTS]
            out = []
            for (name, lat, lon), m, w in zip(SEA_POINTS, marine, wind):
                cm, cw = m.get("current", {}), w.get("current", {})
                if cm.get("wave_height") is None:
                    continue  # grid cell landed on land
                out.append({
                    "name": name, "lat": lat, "lon": lon,
                    "wave": cm.get("wave_height"), "wave_dir": cm.get("wave_direction"),
                    "period": cm.get("wave_period"),
                    "swell": cm.get("swell_wave_height"), "swell_dir": cm.get("swell_wave_direction"),
                    "swell_period": cm.get("swell_wave_period"),
                    "sst": cm.get("sea_surface_temperature"),
                    "wind_kn": cw.get("wind_speed_10m"), "gust_kn": cw.get("wind_gusts_10m"),
                    "wind_dir": cw.get("wind_direction_10m"), "vis_m": cw.get("visibility"),
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "points": out}

        return await self.cache.get("sea", 1800, fetch)

    # ----------------------------------------------------------------- energy
    async def grid(self) -> dict:
        async def fetch() -> dict:
            since = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            mix, ci, demand, fuelinst = await asyncio.gather(
                self._json("https://data.elexon.co.uk/bmrs/api/v1/generation/outturn/current"),
                self._json("https://api.carbonintensity.org.uk/intensity"),
                self._json("https://data.elexon.co.uk/bmrs/api/v1/demand/outturn/summary"),
                # 5-minute FUELINST carries signed interconnector flows (negative = export);
                # the "current" summary above reports them as 0
                self._json("https://data.elexon.co.uk/bmrs/api/v1/datasets/FUELINST/stream",
                           params={"publishDateTimeFrom": since}),
                return_exceptions=True,
            )
            out: dict[str, Any] = {"updated": datetime.now(timezone.utc).isoformat()}
            if not isinstance(mix, Exception):
                out["mix"] = sorted(
                    ({"fuel": m["fuelType"], "mw": m["currentUsage"],
                      "pct": round(m["currentPercentage"], 1)} for m in mix),
                    key=lambda m: -m["mw"],
                )
                out["generation_mw"] = sum(m["mw"] for m in out["mix"])
            if not isinstance(ci, Exception) and ci.get("data"):
                out["carbon"] = ci["data"][0]["intensity"]
            if not isinstance(demand, Exception) and demand:
                out["demand_mw"] = demand[-1].get("demand")
            if not isinstance(fuelinst, Exception):
                rows = fuelinst if isinstance(fuelinst, list) else fuelinst.get("data", [])
                rows = [r for r in rows if str(r.get("fuelType", "")).startswith("INT")]
                if rows:
                    latest = max(r["startTime"] for r in rows)
                    out["interconnectors"] = {r["fuelType"]: r["generation"] for r in rows if r["startTime"] == latest}
                    out["interconnectors_time"] = latest
            return out

        return await self.cache.get("grid", 300, fetch)

    async def power_cuts(self) -> dict:
        async def fetch() -> dict:
            async def ukpn() -> list[dict]:
                d = await self._json(
                    "https://ukpowernetworks.opendatasoft.com/api/explore/v2.1/catalog/"
                    "datasets/ukpn-live-faults/records",
                    params={"limit": 100, "where": "powercuttype != 'Restored'"},
                )
                out = []
                for r in d.get("results", []):
                    gp = r.get("geopoint") or {}
                    if not gp:
                        continue
                    out.append({
                        "dno": "UKPN", "lat": gp.get("lat"), "lon": gp.get("lon"),
                        "type": r.get("powercuttype"), "ref": r.get("incidentreference"),
                        "customers": r.get("nocustomeraffected") or r.get("noplannedcustomers"),
                        "area": r.get("postcodesaffected"),
                        "since": r.get("creationdatetime"),
                        "eta": r.get("estimatedrestorationdate"),
                    })
                return out

            async def ssen() -> list[dict]:
                d = await self._json(
                    "https://ssen-powertrack-api.opcld.com/gridiview/reporter/info/livefaults"
                )
                out = []
                for f in d.get("Faults", []):
                    if f.get("resolved"):
                        continue
                    out.append({
                        "dno": "SSEN", "lat": f.get("latitude"), "lon": f.get("longitude"),
                        "type": f"Unplanned ({f.get('type')})" if f.get("type") in ("HV", "LV") else f.get("type"),
                        "ref": f.get("reference"),
                        "customers": f.get("affectedCustomerCount"),
                        "area": f.get("name") or f.get("postcode"),
                        "since": f.get("loggedAt"),
                        "eta": f.get("estimatedRestoration"),
                    })
                return out

            res = await asyncio.gather(ukpn(), ssen(), return_exceptions=True)
            cuts, errors = [], []
            for name, r in zip(("UKPN", "SSEN"), res):
                if isinstance(r, Exception):
                    errors.append(f"{name}: {r}")
                else:
                    cuts.extend(c for c in r if c["lat"] is not None)
            return {"updated": datetime.now(timezone.utc).isoformat(), "cuts": cuts, "errors": errors}

        return await self.cache.get("power_cuts", 300, fetch)

    # ------------------------------------------------------- space & geology
    async def space(self) -> dict:
        async def fetch() -> dict:
            kp, alerts, aw = await asyncio.gather(
                self._json("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"),
                self._json("https://services.swpc.noaa.gov/products/alerts.json"),
                self._text("https://aurorawatch-api.lancs.ac.uk/0.2/status/current-status.xml"),
                return_exceptions=True,
            )
            out: dict[str, Any] = {"updated": datetime.now(timezone.utc).isoformat()}
            if not isinstance(kp, Exception) and kp:
                last = kp[-1]
                # older format was a list of lists with a header row
                if isinstance(last, list):
                    out["kp"] = float(last[1])
                    out["kp_time"] = last[0]
                else:
                    out["kp"] = last.get("Kp")
                    out["kp_time"] = last.get("time_tag")
            if not isinstance(alerts, Exception):
                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                recent = []
                for a in alerts[:20]:
                    try:
                        issued = datetime.fromisoformat(a["issue_datetime"].replace(" ", "T")).replace(tzinfo=timezone.utc)
                    except (KeyError, ValueError):
                        continue
                    if issued < cutoff:
                        continue
                    msg = a.get("message", "")
                    m = re.search(r"(ALERT|WARNING|WATCH|SUMMARY|EXTENDED WARNING):\s*(.+)", msg)
                    recent.append({"issued": issued.isoformat(), "text": m.group(0) if m else msg[:120]})
                out["alerts"] = recent[:6]
            if not isinstance(aw, Exception):
                m = re.search(r'status_id="(\w+)"', aw)
                out["aurorawatch"] = m.group(1) if m else None
            return out

        return await self.cache.get("space", 600, fetch)

    async def quakes(self) -> dict:
        async def fetch() -> dict:
            xml = await self._text("https://quakes.bgs.ac.uk/feeds/MhSeismology.xml")
            root = ET.fromstring(xml)
            geo = "{http://www.w3.org/2003/01/geo/wgs84_pos#}"
            out = []
            for it in root.iter("item"):
                desc = it.findtext("description") or ""
                mag = re.search(r"Magnitude:\s*([\d.\-]+)", desc)
                depth = re.search(r"Depth:\s*([\d.]+)", desc)
                loc = re.search(r"Location:\s*([^;]+)", desc) or re.search(
                    r"M\s*[\d.]+\s*:(.+?),\s*\w{3}, \d", it.findtext("title") or ""
                )
                when = re.search(r"Origin date/time:\s*([^;]+)", desc)
                try:
                    lat = float(it.findtext(f"{geo}lat"))
                    lon = float(it.findtext(f"{geo}long"))
                except (TypeError, ValueError):
                    continue
                out.append({
                    "lat": lat, "lon": lon,
                    "mag": float(mag.group(1)) if mag else None,
                    "depth": float(depth.group(1)) if depth else None,
                    "place": loc.group(1).strip().title() if loc else "",
                    "time": _rfc822((when.group(1).strip() + " +0000") if when else None),
                    "link": it.findtext("link"),
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "quakes": out}

        return await self.cache.get("quakes", 900, fetch)

    # ------------------------------------------------------------------ roads
    async def _nh(self, closure_type: str) -> dict:
        last: Exception | None = None
        for key in self.nh_keys:
            headers = {**UA, "Ocp-Apim-Subscription-Key": key, "X-Response-MediaType": "application/json"}
            async with self.session.get(
                "https://api.data.nationalhighways.co.uk/roads/v2.0/closures",
                params={"closureType": closure_type}, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status in (401, 403):
                    last = RuntimeError(f"key rejected ({r.status})")
                    continue  # try the secondary key
                r.raise_for_status()
                return await r.json(content_type=None)
        raise last or RuntimeError("no National Highways key")

    async def roads(self) -> dict:
        if not self.nh_keys:
            return {"error": "no_key", "events": []}

        async def fetch() -> dict:
            planned, unplanned = await asyncio.gather(
                self._nh("planned"), self._nh("unplanned"), return_exceptions=True
            )
            events, errors = [], []
            for kind, res in (("unplanned", unplanned), ("planned", planned)):
                if isinstance(res, Exception):
                    errors.append(f"{kind}: {res}")
                    continue
                events.extend(_parse_datex(res, kind))
            return {"updated": datetime.now(timezone.utc).isoformat(), "events": events, "errors": errors}

        return await self.cache.get("roads", 180, fetch)

    async def meteors(self) -> dict:
        async def fetch() -> dict:
            today = datetime.now(timezone.utc).date()
            days = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]

            async def day(d: str) -> list[dict]:
                data = await self._json(
                    "https://api.ukmeteors.co.uk/matches",
                    params={"reqtyp": "summary", "reqval": d},
                )
                return data if isinstance(data, list) else []  # {"result": "no data"}

            res = await asyncio.gather(*(day(d) for d in days), return_exceptions=True)
            out = []
            for r in res:
                if isinstance(r, Exception):
                    continue
                for m in r:
                    try:
                        name = m["orbname"]
                        ts = datetime.strptime(name[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                        out.append({
                            "id": name,
                            "time": ts.isoformat(),
                            "mag": round(m["_mag"], 1),
                            "shower": m.get("_stream"),
                            "name": (m.get("name") or m.get("_stream") or "").strip(),
                            "lat1": round(m["_lat1"], 4), "lon1": round(m["_lng1"], 4), "h1": round(m["_H1"], 1),
                            "lat2": round(m["_lat2"], 4), "lon2": round(m["_lng2"], 4), "h2": round(m["_H2"], 1),
                            "vg": round(m.get("_vg", 0), 1),
                            "dur": round(m.get("_dur", 0), 2),
                            "stations": m.get("numstats"),
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
            out.sort(key=lambda m: m["time"])
            return {"updated": datetime.now(timezone.utc).isoformat(), "meteors": out}

        return await self.cache.get("meteors", 1800, fetch)

    # ------------------------------------------------------------------- news
    async def news(self) -> dict:
        async def fetch() -> dict:
            async def feed(region: str, url: str) -> list[dict]:
                root = ET.fromstring(await self._text(url))
                out = []
                for it in root.iter("item"):
                    title = (it.findtext("title") or "").strip()
                    desc = (it.findtext("description") or "").strip()
                    out.append({
                        "region": region,
                        "title": title,
                        "summary": desc,
                        "link": (it.findtext("link") or "").split("?")[0],
                        "published": _rfc822(it.findtext("pubDate")),
                    })
                return out

            feeds = {**BBC_FEEDS, **self.cfg.bbc_local_feeds}
            res = await asyncio.gather(*(feed(r, u) for r, u in feeds.items()), return_exceptions=True)
            seen: set[str] = set()
            items = []
            for r in res:
                if isinstance(r, Exception):
                    continue
                for it in r:
                    if it["link"] in seen or "/sport/" in it["link"] or "/cymrufyw/" in it["link"]:
                        continue
                    seen.add(it["link"])
                    text = f'{it["title"]} {it["summary"]}'
                    it["severity"] = 3 if NEWS_HIGH.search(text) else 2 if NEWS_MED.search(text) else 1
                    for place in PLACE_ORDER:
                        if re.search(rf"\b{re.escape(place)}\b", text):
                            it["place"] = place
                            it["lat"], it["lon"] = PLACES[place]
                            break
                    items.append(it)
            items.sort(key=lambda i: i.get("published") or "", reverse=True)
            return {"updated": datetime.now(timezone.utc).isoformat(), "items": items}

        return await self.cache.get("news", 300, fetch)

    # --------------------------------------------------------------- maritime
    async def vessels(self, show_all: bool) -> dict:
        if not self.ais:
            return {"error": "no_key", "vessels": []}
        return self.ais.snapshot(show_all)


# ======================================================= emergency helpers
def _norm_station(name: str) -> str:
    n = name.lower().replace("&", "and")
    n = re.sub(r"\b(rnli|lifeboat|lifeboats|station|inshore|all[- ]weather|rescue|boathouse|boat house|the|old)\b", " ", n)
    n = re.sub(r"[^a-z ]", " ", n)
    return " ".join(n.split())


def _dsfrs_field(row: str, name: str) -> str | None:
    m = re.search(
        r'views-field-field-' + name + r'"(?:(?!views-field-field-).)*?field-content">(.*?)</(?:div|h2)>',
        row, re.S,
    )
    if not m:
        return None
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() or None


def _parse_dsfrs(page: str) -> list[dict]:
    out = []
    for part in page.split('<div class="views-row">')[1:]:
        row = part.split("\n", 1)[0]
        link = re.search(r'href="(/incidents/[^"]+)"', row)
        if not link:
            continue
        date, tm = _dsfrs_field(row, "incident-start-date"), _dsfrs_field(row, "incident-start-date-1")
        when = None
        if date and tm:
            try:
                when = datetime.strptime(f"{date} {tm.upper()}", "%d %b %Y %I.%M%p").replace(tzinfo=UK_TZ).isoformat()
            except ValueError:
                pass
        out.append({
            "link": "https://www.dsfire.gov.uk" + link.group(1),
            "status": _dsfrs_field(row, "incident-status"),
            "title": _dsfrs_field(row, "public-title"),
            "location": _dsfrs_field(row, "incident-civil-parish"),
            "stations": _dsfrs_field(row, "attending-station"),
            "type": _dsfrs_field(row, "incident-type"),
            "time": when,
        })
    return out


# ============================================================ DATEX II
def _walk(obj: Any, found: dict) -> None:
    """Collect geometry and road names from a DATEX II location tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "posList" and isinstance(v, str):
                found["lines"].append(v)
            elif k == "roadName" and isinstance(v, str):
                found["roads"].add(v)
            elif k == "locationDescription" and isinstance(v, str):
                found["desc"].append(v)
            elif k == "lanesStatus" and v == "closed":
                found["lane_closed"] = True
            elif k == "latitude" and isinstance(v, (int, float)) and "longitude" in obj:
                found["points"].append((v, obj["longitude"]))
            else:
                _walk(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, found)


def _humanise(camel: str) -> str:
    """'laneClosures' -> 'Lane closures', 'Accident' -> 'Accident'."""
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", camel).strip().lower()
    return words[:1].upper() + words[1:]


def _parse_datex(payload: dict, kind: str) -> list[dict]:
    out = []
    for sit in (payload.get("D2Payload") or {}).get("situation", []):
        for rec in sit.get("situationRecord", []):
            if not rec:
                continue
            rtype, r = next(iter(rec.items()))
            validity = r.get("validity", {})
            status = validity.get("validityStatus")
            if kind == "planned" and status != "active":
                continue
            found = {"lines": [], "roads": set(), "desc": [], "points": [], "lane_closed": False}
            _walk(r.get("locationReference", {}), found)
            lines = []
            for pos in found["lines"][:6]:
                nums = [float(n) for n in pos.split()]
                pts = [[round(nums[i], 5), round(nums[i + 1], 5)] for i in range(0, len(nums) - 1, 2)]
                step = max(1, len(pts) // 40)
                lines.append(pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else []))
            if not lines and found["points"]:
                lines = [[list(found["points"][0])]]
            if not lines:
                continue
            mgmt = (r.get("roadOrCarriagewayOrLaneManagementType") or {}).get("value")
            comments = [c.get("comment") for c in r.get("generalPublicComment", []) if c.get("comment")]
            times = validity.get("validityTimeSpecification", {})
            out.append({
                "id": r.get("idG"),
                "kind": kind,
                "type": _humanise(
                    mgmt if rtype == "sitRoadOrCarriagewayOrLaneManagement" and mgmt and mgmt != "other"
                    else rtype.removeprefix("sit")
                ),
                "source": (r.get("source") or {}).get("sourceIdentification"),
                "cause": (r.get("cause") or {}).get("causeType"),
                "closure": mgmt in ("carriagewayClosures", "roadClosed") or found["lane_closed"],
                "full_closure": mgmt in ("carriagewayClosures", "roadClosed"),
                "roads": sorted(found["roads"]),
                "where": found["desc"][0] if found["desc"] else "",
                "comment": " ".join(comments)[:500],
                "start": times.get("overallStartTime"),
                "end": times.get("overallEndTime"),
                "severity": r.get("severity"),
                "lines": lines,
            })
    return out


# ============================================================== AIS relay
AIS_SPECIAL_TYPES = {35: "military", 51: "sar", 55: "police", 58: "medical"}


def classify_vessel(v: dict) -> str:
    mmsi = str(v.get("mmsi", ""))
    name = (v.get("name") or "").upper()
    if mmsi.startswith(("970", "972", "974")):
        return "distress"  # AIS-SART / MOB / EPIRB
    if mmsi.startswith("111"):
        return "sar_aircraft"
    if name.startswith("RNLI") or " RNLB " in f" {name} ":
        return "rnli"
    if name.startswith(("HMCG", "COASTGUARD")):
        return "sar"
    if name.startswith(("HMS ", "RFA ", "HMNB")):
        return "military"
    if name.startswith(("HMC ", "BORDER FORCE")):
        return "government"
    if name.startswith("POLICE"):
        return "police"
    if name.startswith(("NLV ", "THV ", "CEFAS", "RV ")):
        return "government"
    return AIS_SPECIAL_TYPES.get(v.get("shiptype"), "civil")


class AisRelay:
    """Holds an aisstream.io websocket open while the map is being viewed."""

    IDLE_DISCONNECT = 600  # seconds without a viewer before we hang up
    MAX_AGE = 1800  # drop vessels not heard for 30 minutes

    def __init__(self, owner: "Situation", key: str) -> None:
        self.owner = owner
        self.key = key
        self.vessels: dict[int, dict] = {}
        self.last_request = 0.0
        self.task: asyncio.Task | None = None
        self.status = "idle"

    def snapshot(self, show_all: bool) -> dict:
        self.last_request = time.monotonic()
        if not self.task or self.task.done():
            self.task = self.owner.spawn(self._run())
        now = time.time()
        out = []
        for mmsi, v in list(self.vessels.items()):
            if now - v["ts"] > self.MAX_AGE:
                del self.vessels[mmsi]
                continue
            if "lat" not in v:
                continue
            v["cls"] = classify_vessel(v)
            if show_all or v["cls"] != "civil":
                out.append(v)
        return {"status": self.status, "count": len(self.vessels), "vessels": out}

    async def _run(self) -> None:
        session = self.owner.session
        backoff = 5
        while time.monotonic() - self.last_request < self.IDLE_DISCONNECT:
            try:
                self.status = "connecting"
                async with session.ws_connect(
                    "wss://stream.aisstream.io/v0/stream", heartbeat=30, timeout=aiohttp.ClientWSTimeout(ws_close=10)
                ) as ws:
                    await ws.send_str(json.dumps({
                        "APIKey": self.key,
                        "BoundingBoxes": [[[BBOX[0], BBOX[1]], [BBOX[2], BBOX[3]]]],
                        "FilterMessageTypes": [
                            "PositionReport", "StandardClassBPositionReport",
                            "ExtendedClassBPositionReport", "ShipStaticData",
                            "StaticDataReport", "StandardSearchAndRescueAircraftReport",
                        ],
                    }))
                    self.status = "connected"
                    backoff = 5
                    async for msg in ws:
                        if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                            self._handle(msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
                        if time.monotonic() - self.last_request > self.IDLE_DISCONNECT:
                            break
            except Exception as err:  # noqa: BLE001
                self.status = f"error: {err}"
                _LOGGER.warning("AIS stream error: %s", err)
            if time.monotonic() - self.last_request >= self.IDLE_DISCONNECT:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
        self.status = "idle"

    def _handle(self, raw: str | bytes) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if "error" in m:
            self.status = f"error: {m['error']}"
            return
        meta = m.get("MetaData", {})
        mmsi = meta.get("MMSI")
        if not mmsi:
            return
        mtype = m.get("MessageType")
        body = m.get("Message", {}).get(mtype, {})
        v = self.vessels.setdefault(mmsi, {"mmsi": mmsi})
        v["ts"] = time.time()
        name = (meta.get("ShipName") or "").strip()
        if name:
            v["name"] = name
        if mtype in ("PositionReport", "StandardClassBPositionReport",
                     "ExtendedClassBPositionReport", "StandardSearchAndRescueAircraftReport"):
            v["lat"] = meta.get("latitude")
            v["lon"] = meta.get("longitude")
            v["sog"] = body.get("Sog")
            v["cog"] = body.get("Cog")
            hdg = body.get("TrueHeading")
            v["hdg"] = hdg if hdg is not None and hdg < 360 else None
            if "NavigationalStatus" in body:
                v["nav"] = body["NavigationalStatus"]
            if mtype == "StandardSearchAndRescueAircraftReport":
                v["alt"] = body.get("Altitude")
            if mtype == "ExtendedClassBPositionReport" and body.get("Type"):
                v["shiptype"] = body["Type"]
        elif mtype == "ShipStaticData":
            v["shiptype"] = body.get("Type")
            v["callsign"] = (body.get("CallSign") or "").strip()
            v["dest"] = (body.get("Destination") or "").strip()
        elif mtype == "StaticDataReport":
            rb = body.get("ReportB") or {}
            if rb.get("Valid"):
                v["shiptype"] = rb.get("ShipType")
                v["callsign"] = (rb.get("CallSign") or "").strip()
