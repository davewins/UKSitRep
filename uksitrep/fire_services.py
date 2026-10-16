"""Fire and rescue service incident feeds beyond Devon & Somerset.

Each service publishes differently (RSS, WordPress JSON), and most give the
location only as text, so places are geocoded with postcodes.io and must fall
inside the service's own area. Where a service only names the responding
station, the incident is placed at that station and marked approximate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import logging
import re
import xml.etree.ElementTree as ET

import aiohttp
from . import USER_AGENT

_LOGGER = logging.getLogger("uksitrep")

UA = {"User-Agent": f"Mozilla/5.0 ({USER_AGENT})"}
KEEP_DAYS = 14  # several services only post every few days
FIRE_TIMEOUT = aiohttp.ClientTimeout(total=30)
PER_SERVICE = 40

# id: (name, bounding box S, W, N, E)
FIRE_SERVICES = {
    "avon": ("Avon FRS", (51.25, -3.15, 51.70, -2.20)),
    "bucks": ("Buckinghamshire FRS", (51.45, -1.20, 52.20, -0.45)),
    "cheshire": ("Cheshire FRS", (52.90, -3.15, 53.50, -1.95)),
    "cleveland": ("Cleveland FB", (54.45, -1.50, 54.75, -0.75)),
    "hants": ("Hampshire & IoW FRS", (50.55, -1.95, 51.40, -0.70)),
    "rbfrs": ("Royal Berkshire FRS", (51.30, -1.60, 51.65, -0.50)),
    "shropshire": ("Shropshire FRS", (52.30, -3.25, 53.00, -2.20)),
}

SETTLEMENT_RANK = {"City": 0, "Town": 1, "Village": 2, "Suburban Area": 3, "Hamlet": 4, "Other Settlement": 5}
# capitalised words that are never places
NOT_PLACES = {
    "Firefighters", "Firefighter", "Crews", "Crew", "Fire", "Rescue", "Service", "The", "A", "An", "One", "Two",
    "Three", "Four", "Five", "At", "On", "In", "Upon", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday", "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "Road", "Street", "Lane", "Avenue", "Close", "Drive", "Way", "Hill",
    "House", "Car", "Vehicle", "Kitchen", "Shed", "Garage", "Boat", "Grass", "Bin", "Rubbish", "Flat", "Building",
    "Royal", "Berkshire", "Hampshire", "Cheshire", "Buckinghamshire", "Shropshire", "Avon", "Cleveland", "Officer",
    "Carbon", "Monoxide", "Leak", "Smoke", "Chimney", "Property", "Incident", "Update", "RTC", "AFA", "Gas",
}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _place_candidates(*texts: str) -> list[str]:
    """Likely place names in incident text, best guesses first."""
    out: list[str] = []
    word = r"[A-Z][\w'’-]+"
    phrase = rf"{word}(?:\s+(?:upon|on|under|le|in|by|the|de|{word}))*"
    texts = [t for t in texts if t]
    # "... in Warrington", "... at Bursledon", "... near Ludlow" in any text beat bare capitalised words
    for t in texts:
        for m in reversed(re.findall(rf"\b(?:in|at|near|of)\s+({phrase})", t)):
            out.append(m)
    for t in texts:
        # otherwise capitalised runs, trying the trailing words first ("... Norton Stockton" -> "Stockton")
        for run in reversed(re.findall(phrase, t)):  # addresses end with the town
            words = [w for w in run.split() if w not in NOT_PLACES]
            for n in (1, 2):
                for i in range(len(words) - n, -1, -1):
                    out.append(" ".join(words[i:i + n]))
    seen, res = set(), []
    for c in out:
        c = c.strip(" ,.'’")
        words = [w for w in c.split() if w not in NOT_PLACES]
        c = " ".join(words)
        if len(c) >= 3 and c not in seen:
            seen.add(c)
            res.append(c)
    return res[:8]


class FireServices:
    """Mixin for Situation."""

    async def _place_in(self, name: str, box: tuple[float, float, float, float]) -> dict | None:
        """Geocode a settlement name, only accepting matches inside box."""
        key = f"{name.lower()}|{box}"
        if key in self.place_cache:
            return self.place_cache[key]
        try:
            d = await self._json("https://api.postcodes.io/places", params={"q": name, "limit": "20"})
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("geocode %s failed: %s", name, err)
            return None  # transient: don't cache
        best = None
        for c in d.get("result") or []:
            lat, lon = c.get("latitude"), c.get("longitude")
            if lat is None or not (box[0] <= lat <= box[2] and box[1] <= lon <= box[3]):
                continue
            n1 = (c.get("name_1") or "").lower()
            if n1 != name.lower() and not n1.startswith(name.lower() + "-"):  # "Stockton" -> Stockton-on-Tees
                continue
            rank = SETTLEMENT_RANK.get(c.get("local_type"), 9)
            if best is None or rank < best[0]:
                best = (rank, {"lat": lat, "lon": lon, "place": c.get("name_1"),
                               "area": c.get("district_borough") or c.get("county_unitary")})
        res = best[1] if best else None
        self.place_cache[key] = res
        return res

    async def _locate(self, svc: str, *texts: str) -> dict | None:
        box = FIRE_SERVICES[svc][1]
        for cand in _place_candidates(*texts):
            p = await self._place_in(cand, box)
            if p:
                return p
        return None

    async def _rss(self, url: str) -> list[ET.Element]:
        async with self.session.get(url, headers=UA, timeout=FIRE_TIMEOUT) as r:
            r.raise_for_status()
            return list(ET.fromstring(await r.read()).iter("item"))

    async def _wp(self, url: str, **params) -> list[dict]:
        return await self._json(url, params={"per_page": str(PER_SERVICE), **params}, headers=UA,
                                timeout=FIRE_TIMEOUT)

    @staticmethod
    def _utc(s: str | None, local: bool = False) -> str | None:
        """ISO or RFC 822 date -> ISO UTC. WordPress 'date_gmt' has no zone but is UTC."""
        if not s:
            return None
        try:
            dt = parsedate_to_datetime(s) if "," in s else datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    # ---------------------------------------------------------- per service
    async def _fire_cheshire(self) -> list[dict]:
        out = []
        for it in await self._rss("https://www.cheshirefire.gov.uk/duocms/api/incidents/rss"):
            title, desc = _clean(it.findtext("title")), _clean(it.findtext("description"))
            out.append({"title": title, "summary": desc, "time": self._utc(it.findtext("pubDate")),
                        "link": it.findtext("link"), "where": [title, desc]})
        return out

    async def _fire_bucks(self) -> list[dict]:
        out = []
        for it in await self._rss("https://bucksfire.gov.uk/incidents/feed/"):
            title, desc = _clean(it.findtext("title")), _clean(it.findtext("description"))
            # Bucks name the crews, not the place: use the first crew's station
            m = re.search(r"\bfrom\s+([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)*)", desc)
            out.append({"title": title, "summary": desc, "time": self._utc(it.findtext("pubDate")),
                        "link": it.findtext("link"), "where": [m.group(1)] if m else [],
                        "approx": f"{m.group(1)} fire station" if m else None})
        return out

    async def _fire_wp(self, base: str, rest: str, title_is_place: bool = False) -> list[dict]:
        out = []
        for x in await self._wp(f"{base}/wp-json/wp/v2/{rest}", _embed="wp:term"):
            title = _clean((x.get("title") or {}).get("rendered"))
            body = _clean((x.get("content") or {}).get("rendered") or (x.get("excerpt") or {}).get("rendered"))
            terms = {t.get("taxonomy"): t.get("name") for grp in (x.get("_embedded") or {}).get("wp:term", [])
                     for t in grp if isinstance(t, dict)}
            out.append({"title": title, "summary": body[:400], "time": self._utc(x.get("date_gmt")),
                        "link": x.get("link"), "terms": terms,
                        "where": [title, body[:300]] if not title_is_place else [title, terms.get("location", "")]})
        return out

    async def _fire_shropshire(self) -> list[dict]:
        out = []
        for row in await self._fire_wp("https://www.shropshirefire.gov.uk", "incidents"):
            t = row["terms"]
            station = t.get("station_ground") or t.get("stations")
            kind = (t.get("incident_type") or "Incident").title().replace("Afa", "Automatic fire alarm").replace("Rtc", "RTC")
            out.append({**row, "title": kind, "type": kind, "where": [station] if station else [],
                        "approx": f"{station} station area" if station else None})
        return out

    # ---------------------------------------------------------- combined
    async def fire_services(self) -> dict:
        """Incidents from every enabled service except Devon & Somerset (handled in fire())."""
        fetchers = {
            "avon": lambda: self._fire_wp("https://www.avonfire.gov.uk", "karib_incident", title_is_place=True),
            "bucks": self._fire_bucks,
            "cheshire": self._fire_cheshire,
            "cleveland": lambda: self._fire_wp("https://www.clevelandfire.gov.uk", "incidents", title_is_place=True),
            "hants": lambda: self._fire_wp("https://www.hantsfire.gov.uk", "incident"),
            "rbfrs": lambda: self._fire_wp("https://www.rbfrs.co.uk", "incident"),
            "shropshire": self._fire_shropshire,
        }
        enabled = [s for s in fetchers if s in self.cfg.fire_services]

        async def one(svc: str) -> dict:
            async def fetch() -> dict:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
                rows = [r for r in await fetchers[svc]() if r.get("time") and r["time"] >= cutoff]
                rows.sort(key=lambda r: r["time"], reverse=True)
                out = []
                for r in rows[:PER_SERVICE]:
                    p = await self._locate(svc, *[w for w in r.get("where", []) if w])
                    if not p:
                        continue
                    title = r["title"]
                    if FIRE_SERVICES[svc][0].startswith(("Avon", "Cleveland")):
                        # these titles are just the address: describe it from the text instead
                        title = (r["summary"].split(".")[0][:80] or "Incident") if r["summary"] else "Incident"
                    out.append({
                        "svc": svc, "service": FIRE_SERVICES[svc][0], "title": title, "type": r.get("type"),
                        "summary": r["summary"][:300], "location": r["title"] if title != r["title"] else p["place"],
                        "area": p.get("area"), "time": r["time"], "link": r.get("link"),
                        "lat": p["lat"], "lon": p["lon"], "approx": r.get("approx"),
                    })
                return {"incidents": out}

            return await self.cache.get(f"fire:{svc}", 900, fetch)

        results = await asyncio.gather(*(one(s) for s in enabled))
        incidents, errors = [], []
        for svc, res in zip(enabled, results):
            if "error" in res:
                errors.append(f"{FIRE_SERVICES[svc][0]}: {res['error']}")
            incidents.extend(res.get("incidents", []))
        return {"incidents": incidents, "errors": errors,
                "services": {s: FIRE_SERVICES[s][0] for s in enabled}}
