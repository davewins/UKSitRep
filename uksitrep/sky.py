"""Sky watching: public allsky cameras and observatories / telescopes.

- Allsky (Thomas Jacquin's software) owners can opt in to its map, which embeds the
  whole camera list in the page; it's ~2.5 MB, so it is fetched once a day.
- indi-allsky cameras report to allsky-map.com, which has an open JSON camera list
  and serves each camera's latest image.
- Observatories, telescopes and planetaria come from OpenStreetMap, refreshed weekly
  in the background (like the OSM webcams) with a bundled seed for a fresh install.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
import re
import time
from urllib.parse import quote

import aiohttp
from . import USER_AGENT

_LOGGER = logging.getLogger("uksitrep")

ALLSKY_MAP = "https://www.thomasjacquin.com/allsky-map/"
INDI_ALLSKY_MAP = "https://allsky-map.com"
SKY_BOX = (49.8, -8.7, 60.9, 1.9)  # S, W, N, E: UK and Ireland
SKY_UA = {"User-Agent": USER_AGENT}
INDI_ID = re.compile(r"^[0-9a-f]{8,32}$")
INDI_OFFLINE_DAYS = 7  # drop indi-allsky cameras not seen for a week
OBS_QUERY = (
    "[out:json][timeout:120];("
    'nwr["man_made"="observatory"]({b});nwr["man_made"="telescope"]({b});nwr["amenity"="planetarium"]({b});'
    ");out center tags;"
)
# observatories that aren't looking at the sky
NOT_SKY_TYPES = {"meteorological", "tidal", "magnetic", "oceanography", "oceanographic", "seismological",
                 "geophysical", "gravimetric", "weather", "bird", "wildlife", "hydrological", "volcanological"}
NOT_SKY_NAME = re.compile(r"weather|met station|tidal|magnetic|seismo|bird|wildlife|^point d'|^tourelle|beach front", re.I)
# well-known sites that OSM tags as museums or misses: (name, lat, lon, kind, url)
CURATED_SITES = [
    ("Royal Observatory Greenwich", 51.4774, -0.0008, "optical", "https://www.rmg.co.uk/royal-observatory"),
    ("The Observatory Science Centre, Herstmonceux", 50.8680, 0.3370, "optical", "https://www.the-observatory.org/"),
    ("Kielder Observatory", 55.2319, -2.6163, "optical", "https://www.kielderobservatory.org/"),
    ("Mullard Radio Astronomy Observatory", 52.1671, 0.0351, "radio", "https://www.mrao.cam.ac.uk/"),
    ("Jodrell Bank Observatory", 53.2367, -2.3051, "radio", "https://www.jodrellbank.net/"),
    ("Goonhilly Earth Station", 50.0491, -5.1833, "radio", "https://www.goonhilly.org/"),
    ("Armagh Observatory and Planetarium", 54.3530, -6.6498, "optical", "https://armagh.space/"),
    ("Norman Lockyer Observatory", 50.6878, -3.2196, "optical", "https://www.normanlockyer.org/"),
    ("Coats Observatory", 55.8457, -4.4310, "optical", "https://www.renfrewshireleisure.com/coats-observatory/"),
]
# live snapshot cameras shown in a site's popup, keyed by curated site name: (label, image URL), page URL
OBS_CAMS = {
    "Jodrell Bank Observatory": ("https://www.jodrellbank.manchester.ac.uk/about/webcams/", [
        ("Lovell Telescope", "https://www.jodrellbank.manchester.ac.uk/uploads/webcams/lovell/webcam.jpg"),
        ("Mark II", "https://www.jodrellbank.manchester.ac.uk/uploads/webcams/mkii/webcam.jpg"),
        ("7m telescope", "https://www.jodrellbank.manchester.ac.uk/uploads/webcams/7m/webcam.jpg"),
    ]),
}
MERGE_KM = 1.0  # telescopes this close to a named observatory are counted on it instead


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _in_box(lat: float | None, lon: float | None) -> bool:
    return lat is not None and lon is not None and SKY_BOX[0] <= lat <= SKY_BOX[2] and SKY_BOX[1] <= lon <= SKY_BOX[3]


def _http(u: str | None) -> str | None:
    u = (u or "").strip()
    return u if u.startswith(("http://", "https://")) else None


def parse_allsky_pins(page: str) -> list[dict]:
    """Thomas Jacquin's Allsky map page -> UK cameras."""
    i = page.index("let pins = [") + len("let pins = ")
    pins = json.loads(page[i:page.index("];", i) + 1])
    out = []
    for p in pins:
        lat, lon = _f(p.get("latitude")), _f(p.get("longitude"))
        if not _in_box(lat, lon):
            continue
        site, image = _http(p.get("website_url")), _http(p.get("image_url"))
        out.append({
            "id": f"tj{p.get('id')}", "name": (p.get("location") or "Allsky camera").strip(),
            # no website: an http:// image still works as a link, just not inline
            "owner": (p.get("owner") or "").strip(), "lat": lat, "lon": lon, "url": site or image,
            # an http:// image would be blocked on an https page
            "image": image if image and image.startswith("https://") else None,
            "kit": ", ".join(x.strip() for x in (p.get("camera"), p.get("lens")) if x and x.strip()),
            "source": "Allsky",
        })
    return out


def parse_indi_cams(rows: list[dict]) -> list[dict]:
    """allsky-map.com camera list -> UK indi-allsky cameras seen in the last week."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=INDI_OFFLINE_DAYS)).isoformat()
    out = []
    # owners sometimes re-register a camera: keep the online / most recently seen one
    rows = sorted(rows, key=lambda c: (c.get("status") == "online", c.get("lastSeen") or ""), reverse=True)
    for c in rows:
        lat, lon, cid = _f(c.get("lat")), _f(c.get("lng")), str(c.get("id") or "")
        if not _in_box(lat, lon) or not INDI_ID.match(cid) or (c.get("lastSeen") or "") < cutoff:
            continue
        online = c.get("status") == "online"
        if any(o["owner"] == (c.get("owner") or "").strip() and _km(lat, lon, o["lat"], o["lon"]) < 10 for o in out):
            continue
        out.append({
            "id": f"indi{cid}", "name": (c.get("name") or "indi-allsky camera").strip(),
            "owner": (c.get("owner") or "").strip(), "lat": lat, "lon": lon,
            "url": _http(c.get("siteUrl")) or f"{INDI_ALLSKY_MAP}/",
            "image": f"{INDI_ALLSKY_MAP}/api/cameras/{cid}/image" if c.get("imageUrl") and online else None,
            "online": online, "seen": c.get("lastSeen"), "source": "indi-allsky",
        })
    return out


def _km(a: float, b: float, c: float, d: float) -> float:
    x = (math.sin(math.radians(c - a) / 2) ** 2
         + math.cos(math.radians(a)) * math.cos(math.radians(c)) * math.sin(math.radians(d - b) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(x))


def parse_observatories(els: list[dict]) -> list[dict]:
    """Overpass elements -> sky-facing observatories, telescopes and planetaria."""
    sites, scopes = [], []
    for e in els:
        t = e.get("tags", {})
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        name = (t.get("name") or "").strip()
        otype = (t.get("observatory:type") or "").lower()
        if lat is None or otype in NOT_SKY_TYPES or (name and NOT_SKY_NAME.search(name)):
            continue
        ttype = (t.get("telescope:type") or "").lower()
        if t.get("amenity") == "planetarium":
            kind = "planetarium"
        elif ttype == "radio" or re.search(r"\bradio\b", name, re.I):
            kind = "radio"
        else:
            kind = "optical"
        wiki = t.get("wikipedia", "")
        link = (_http(t.get("website")) or _http(t.get("contact:website")) or _http(t.get("url"))
                or (f"https://{wiki.split(':', 1)[0]}.wikipedia.org/wiki/{quote(wiki.split(':', 1)[1].replace(' ', '_'))}"
                    if ":" in wiki else None))
        rec = {
            "id": f"osm{e['type'][0]}{e['id']}", "lat": lat, "lon": lon, "kind": kind,
            "name": name or {"radio": "Radio telescope", "planetarium": "Planetarium"}.get(kind, "Telescope" if t.get("man_made") == "telescope" else "Observatory"),
            "named": bool(name), "operator": t.get("operator"), "url": link,
            "osm": f"https://www.openstreetmap.org/{e['type']}/{e['id']}",
            "public": t.get("access") in ("yes", "permissive", "customers") or t.get("tourism") in ("attraction", "museum"),
            "diameter": t.get("telescope:diameter"), "scopes": 0,
        }
        (scopes if t.get("man_made") == "telescope" else sites).append(rec)
    for name, lat, lon, kind, url in CURATED_SITES:
        near = [o for o in sites if _km(lat, lon, o["lat"], o["lon"]) < 0.5 and o["kind"] != "planetarium"]
        for o in near:
            sites.remove(o)
        sites.append({"id": "cur" + re.sub(r"\W", "", name.lower())[:20], "lat": lat, "lon": lon, "kind": kind,
                      "name": name, "named": True, "operator": next((o["operator"] for o in near if o["operator"]), None),
                      "url": url, "osm": near[0]["osm"] if near else None, "public": True, "diameter": None, "scopes": 0})
    # fold telescopes (e.g. the dishes at Jodrell Bank, Chilbolton) into a nearby named observatory
    for s in scopes:
        host = next((o for o in sites if o["named"] and _km(s["lat"], s["lon"], o["lat"], o["lon"]) < MERGE_KM), None)
        if host:
            host["scopes"] += 1
            if s["kind"] == "radio":
                host["kind"] = "radio"
        else:
            # clusters of unnamed dishes (e.g. Lord's Bridge) become one marker
            twin = None if s["named"] else next(
                (o for o in sites if not o["named"] and o["kind"] == s["kind"] and _km(s["lat"], s["lon"], o["lat"], o["lon"]) < MERGE_KM), None)
            if twin:
                twin["scopes"] += 1
            else:
                sites.append(s)
    for s in sites:
        s.pop("named", None)
    return sites


class SkyWatch:
    """Mixin for Situation."""

    async def allsky(self) -> dict:
        async def tj() -> dict:
            async with self.session.get(ALLSKY_MAP, headers=SKY_UA, timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                page = await r.text()
            return {"cams": await asyncio.to_thread(parse_allsky_pins, page)}

        async def indi() -> dict:
            rows = await self._json(f"{INDI_ALLSKY_MAP}/api/cameras", headers=SKY_UA)
            return {"cams": parse_indi_cams(rows if isinstance(rows, list) else [])}

        # the Allsky page is big and changes slowly; allsky-map.com knows who is online
        a, b = await asyncio.gather(self.cache.get("allsky_tj", 86400, tj), self.cache.get("allsky_indi", 600, indi))
        errors = [f"{n}: {r['error']}" for n, r in (("Allsky map", a), ("allsky-map.com", b)) if "error" in r]
        cams = a.get("cams", []) + b.get("cams", [])
        if not cams and errors:
            return {"error": "; ".join(errors)}
        return {"updated": datetime.now(timezone.utc).isoformat(), "cams": cams, "errors": errors}

    async def observatories(self) -> dict:
        """Served from a saved copy, refreshed weekly in the background (Overpass is slow)."""
        store = self.cfg.data_dir / "observatories.json"
        saved: dict = {"fetched": 0, "sites": []}
        for path in (store, Path(__file__).with_name("data") / "observatories.json"):
            try:
                saved = await asyncio.to_thread(lambda: json.loads(path.read_text()))
                break
            except (OSError, ValueError):
                continue
        if path != store:
            saved["fetched"] = 0  # bundled seed: refresh as soon as possible
        due = time.time() - saved.get("fetched", 0) > 7 * 86400 and time.time() > self.obs_retry_at
        if due and (not self.obs_task or self.obs_task.done()):
            self.obs_task = self.spawn(self._refresh_observatories(store))
        sites = []
        for o in saved.get("sites", []):
            cam = OBS_CAMS.get(o.get("name"))
            sites.append({**o, "cams": [{"label": l, "image": i} for l, i in cam[1]], "cams_url": cam[0]} if cam else o)
        return {"sites": sites, "fetched": saved.get("fetched")}

    async def _refresh_observatories(self, store: Path) -> None:
        from .feeds import OVERPASS_MIRRORS

        query = OBS_QUERY.format(b=",".join(str(x) for x in SKY_BOX))
        for mirror in OVERPASS_MIRRORS:
            try:
                async with self.session.post(mirror, data={"data": query}, headers=SKY_UA,
                                             timeout=aiohttp.ClientTimeout(total=150)) as r:
                    r.raise_for_status()
                    els = (await r.json(content_type=None)).get("elements", [])
            except Exception as err:  # noqa: BLE001
                _LOGGER.info("overpass %s failed: %s", mirror, err)
                continue
            sites = parse_observatories(els)
            if not sites:
                continue
            await asyncio.to_thread(lambda: store.write_text(json.dumps({"fetched": time.time(), "sites": sites})))
            _LOGGER.info("Observatories refreshed: %d sites", len(sites))
            return
        self.obs_retry_at = time.time() + 3600
        _LOGGER.warning("Observatories: all Overpass mirrors failed, retrying in 1 h")
