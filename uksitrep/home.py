"""Home area: a "near me" summary built from the cached feeds, and notifications
of new nearby events to a Home Assistant webhook.

Everything here reads the same cached feed methods the map uses, so it adds no
upstream load beyond what the map already causes (plus one background poll).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
import time
from urllib.parse import quote
from typing import Any

import aiohttp
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger("uksitrep")

NOTIFY_EVERY_S = 300
UK_TZ = ZoneInfo("Europe/London")
NOTIFY_KINDS = ("warning", "flood", "river", "sewage", "fire", "road", "power", "rnli", "aircraft", "hems",
                "lightning", "iss")
SENT_KEEP_DAYS = 7
POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$")
OUTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")
SEARCH_RE = re.compile(r"^[\w\s'’,.()&-]{2,60}$")
PLACE_RANK = {"City": 0, "Town": 1, "Village": 2, "Suburban Area": 3, "Hamlet": 4, "Other Settlement": 5}
PLACE_ZOOM = {"City": 12, "Town": 13, "Suburban Area": 14, "Village": 14, "Hamlet": 15, "Other Settlement": 15}


def _km(a: float, b: float, c: float, d: float) -> float:
    r = math.radians
    x = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def _age_min(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


class HomeArea:
    """Mixin for Situation."""

    async def search(self, q: str) -> dict:
        """Postcode, postcode district or place name -> map locations (postcodes.io, Great Britain)."""
        q = re.sub(r"\s+", " ", q or "").strip()
        if not SEARCH_RE.match(q):
            return {"results": [], "error": "Type a town or postcode"}
        pc = q.upper().replace(" ", "")

        async def fetch() -> dict:
            base = "https://api.postcodes.io"
            if POSTCODE_RE.match(pc) or OUTCODE_RE.match(pc):
                full = bool(POSTCODE_RE.match(pc))
                try:
                    r = (await self._json(f"{base}/{'postcodes' if full else 'outcodes'}/{quote(pc)}"))["result"]
                except aiohttp.ClientResponseError as err:
                    if err.status != 404:
                        raise
                    r = None
                if r and r.get("latitude") is not None:
                    area = r.get("admin_district")
                    area = ", ".join(area[:3]) if isinstance(area, list) else area
                    return {"results": [{"name": r.get("postcode") or r.get("outcode"), "area": area,
                                         "kind": "Postcode" if full else "Postcode district",
                                         "lat": r["latitude"], "lon": r["longitude"], "zoom": 16 if full else 13}]}
                if full:
                    return {"results": [], "error": f"Postcode {q.upper()} not found"}
            d = await self._json(f"{base}/places", params={"q": q, "limit": "40"})
            seen, out = set(), []
            for r in d.get("result") or []:
                if r.get("latitude") is None:
                    continue
                n1, n2 = r.get("name_1") or "", r.get("name_2") or ""
                # Welsh and Gaelic places come back under their local name first
                name = n2 if n2 and r.get("name_1_lang") and r.get("name_2_lang") == "eng" else n1
                alt = n1 if name != n1 else (n2 or None)
                area = r.get("district_borough") or r.get("county_unitary") or r.get("region")
                key = (name.lower(), area)
                if key in seen:
                    continue
                seen.add(key)
                exact = q.lower() in (n1.lower(), n2.lower())
                out.append(((not exact, PLACE_RANK.get(r.get("local_type"), 9)), {
                    "name": name, "alt": alt, "area": area, "region": r.get("region") or r.get("country"),
                    "kind": r.get("local_type") or "Place", "lat": r["latitude"], "lon": r["longitude"],
                    "zoom": PLACE_ZOOM.get(r.get("local_type"), 14)}))
            out.sort(key=lambda x: x[0])
            return {"results": [x[1] for x in out[:8]]}

        return await self.cache.get(f"search:{q.lower()}", 86400, fetch)

    def _near(self, items: list[dict], radius: float | None = None, lat: str = "lat", lon: str = "lon") -> list[dict]:
        h = self.cfg
        radius = radius or h.home_radius_km
        out = []
        for it in items or []:
            la, lo = it.get(lat), it.get(lon)
            if la is None or lo is None:
                continue
            d = _km(h.home_lat, h.home_lon, float(la), float(lo))
            if d <= radius:
                out.append({**it, "km": round(d, 1)})
        return sorted(out, key=lambda x: x["km"])

    async def home_summary(self) -> dict:
        cfg = self.cfg
        if cfg.home_lat is None or cfg.home_lon is None:
            return {"error": "disabled (set HOME_LAT and HOME_LON)"}
        return await self.cache.get("home", 60, self._home_summary)

    async def _home_summary(self) -> dict:
        cfg = self.cfg

        async def safe(coro):
            try:
                # a slow upstream mustn't hold up the panel; it keeps loading in the background
                # (shielded) so the next summary finds it in the cache
                res = await asyncio.wait_for(asyncio.shield(self.spawn(coro)), 12)
                return res if isinstance(res, dict) else {}
            except Exception as err:  # noqa: BLE001 - one broken feed mustn't blank the panel
                _LOGGER.debug("home summary: %s", err)
                return {}

        lat, lon, r = cfg.home_lat, cfg.home_lon, cfg.home_radius_km
        dlat, dlon = r / 111, r / (111 * math.cos(math.radians(lat)))
        (rivers, floods, sewage, bathing, fire, roads, cuts, warnings, rnli, aircraft, lightning, iss, fuel,
         tides, air, rail_dis) = await asyncio.gather(
            safe(self.rivers()), safe(self.floods()), safe(self.sewage()), safe(self.bathing()), safe(self.fire()),
            safe(self.roads()), safe(self.power_cuts()), safe(self.weather_warnings()), safe(self.rnli()),
            safe(self.aircraft()), safe(self.lightning()), safe(self.iss(lat, lon)),
            safe(self.fuel((lat - dlat, lon - dlon, lat + dlat, lon + dlon))), safe(self.tide_stations()),
            safe(self.air_quality()), safe(self.rail_disruptions()),
        )
        out: dict[str, Any] = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "home": {"name": cfg.home_name, "lat": lat, "lon": lon, "radius_km": r},
        }
        # Met Office warnings are per region: count any whose region centre is within ~150 km
        out["warnings"] = [{"title": w["title"], "level": w["level"], "link": w.get("link"), "km": w["km"]}
                           for w in self._near(warnings.get("warnings", []), 150)]
        out["floods"] = [{"id": f.get("id"), "area": f.get("area"), "severity": f.get("severity"),
                          "level": f.get("level"), "km": f["km"], "lat": f["lat"], "lon": f["lon"]}
                         for f in self._near(floods.get("floods", []))]
        gauges = self._near(rivers.get("gauges", []))
        pick = lambda g: {k: g.get(k) for k in ("id", "name", "river", "level", "low", "high", "state", "trend", "km",
                                                  "lat", "lon", "time")}
        out["rivers"] = {"raised": [pick(g) for g in gauges if g["state"] in ("high", "record")],
                         "nearest": [pick(g) for g in gauges[:5]]}
        ov = self._near(sewage.get("overflows", []))
        out["sewage"] = {
            "active": [{"id": o["id"], "water": o.get("water"), "company": o.get("company"), "start": o.get("start"),
                        "km": o["km"], "lat": o["lat"], "lon": o["lon"]} for o in ov if o.get("active")],
            "recent": [{"id": o["id"], "water": o.get("water"), "end": o.get("end"), "km": o["km"]}
                       for o in ov if not o.get("active") and _age_min(o.get("end")) < 48 * 60],
        }
        sites = self._near(bathing.get("sites", []))[:6]
        for b in sites:
            b["sewage_near"] = sum(1 for o in sewage.get("overflows", [])
                                   if (o.get("active") or _age_min(o.get("end")) < 48 * 60)
                                   and _km(b["lat"], b["lon"], o["lat"], o["lon"]) < 3)
        out["bathing"] = [{k: b.get(k) for k in ("name", "classification", "risk", "sewage_near", "km", "url")}
                          for b in sites]
        out["fire"] = [{"title": f.get("title"), "location": f.get("location"), "time": f.get("time"),
                        "service": f.get("service"), "link": f.get("link"), "km": f["km"]}
                       for f in self._near(fire.get("incidents", [])) if _age_min(f.get("time")) < 7 * 1440]
        road_pts = []
        for e in roads.get("events", []):
            pts = [p for line in e.get("lines") or [] for p in line]
            if pts:
                best = min(pts, key=lambda p: _km(lat, lon, p[0], p[1]))
                road_pts.append({**e, "lat": best[0], "lon": best[1]})
        out["roads"] = [{"id": e.get("id"), "type": e.get("type"), "where": e.get("where"), "roads": e.get("roads"),
                         "kind": e.get("kind"), "closure": e.get("full_closure"), "km": e["km"]}
                        for e in self._near(road_pts)][:10]
        out["power"] = [{"ref": c.get("ref"), "dno": c.get("dno"), "type": c.get("type"), "customers": c.get("customers"),
                         "eta": c.get("eta"), "km": c["km"]} for c in self._near(cuts.get("cuts", []))]
        out["rnli"] = [{"id": x.get("id"), "station": x.get("station"), "time": x.get("time"), "km": x["km"]}
                       for x in self._near(rnli.get("launches", []), r * 2) if _age_min(x.get("time")) < 1440]
        planes = aircraft.get("aircraft", [])
        out["aircraft"] = {
            "emergencies": [{"hex": a["hex"], "flight": a.get("flight"), "squawk": a.get("squawk"),
                             "emergency": a.get("emergency"), "alt": a.get("alt"), "km": a["km"]}
                            for a in self._near(planes, r * 2) if a.get("emergency")],
            "hems": [{"hex": e["hex"], "flight": e.get("flight"), "kind": e.get("kind"), "first": e.get("first"),
                      "km": e["km"]} for e in self._near(aircraft.get("hems_events", []))],
            "overhead": len(self._near(planes, 10)),
        }
        now_s = time.time()
        strikes = [{"lat": la, "lon": lo, "t": t} for t, la, lo in lightning.get("strikes", []) if now_s - t < 1800]
        near_strikes = self._near(strikes)
        out["lightning"] = {"count_30min": len(near_strikes), "nearest_km": near_strikes[0]["km"] if near_strikes else None,
                            "count_15min": sum(1 for s in near_strikes if now_s - s["t"] < 900)}
        out["iss"] = {"passes": (iss.get("passes") or [])[:3]}
        st = [s for s in fuel.get("stations", []) if not s.get("closed")]
        cheapest = {}
        for ft in ("E10", "B7_STANDARD"):
            have = [s for s in self._near(st) if ft in s.get("prices", {})]
            if have:
                s = min(have, key=lambda s: (s["prices"][ft], s["km"]))
                cheapest[ft] = {"price": s["prices"][ft], "name": s.get("name"), "brand": s.get("brand"),
                                "km": s["km"], "lat": s["lat"], "lon": s["lon"]}
        out["fuel"] = cheapest
        near_tide = self._near(tides.get("stations", []), 40)
        if near_tide:
            t = await safe(self.tides(near_tide[0]["id"]))
            nxt = [e for e in t.get("events", []) if _age_min(e["time"]) < 0][:3]
            out["tide"] = {"station": near_tide[0]["name"], "km": near_tide[0]["km"], "next": nxt}
        pts = air.get("points", [])
        if pts:
            p = min(pts, key=lambda p: _km(lat, lon, p["lat"], p["lon"]))
            out["air"] = {"name": p["name"], "aqi": p.get("aqi"),
                          "pollen": {k: v for k, v in (p.get("pollen") or {}).items() if v}}
        if self.cfg.rail_ldb_key:
            codes = await self.home_stations()
            boards = await asyncio.gather(*(safe(self.rail_board(c)) for c in codes))
            out["rail"] = {"boards": [{"crs": b["crs"], "name": b["name"], "services": b["services"][:5],
                                       "messages": b.get("messages", [])[:2]} for b in boards if b.get("services") is not None]}
        dis = [d for d in rail_dis.get("disruptions", [])
               if any(_km(lat, lon, s["lat"], s["lon"]) <= r for s in d.get("stations", []))]
        if dis:
            out.setdefault("rail", {})["disruptions"] = [{k: d.get(k) for k in ("id", "kind", "text", "url")} for d in dis[:5]]
        return out

    # ------------------------------------------------------------ notifications
    def _home_events(self, s: dict) -> list[tuple[str, str, str, str, dict]]:
        """(kind, key, title, message, extra) for everything currently notable near home."""
        ev: list[tuple[str, str, str, str, dict]] = []
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for w in s.get("warnings", []):
            if w["level"] in ("amber", "red"):
                ev.append(("warning", f"warn:{w['title']}", f"Met Office {w['level']} warning", w["title"], {}))
        for f in s.get("floods", []):
            ev.append(("flood", f"flood:{f['id']}:{f['level']}", f"{f['severity']}", f"{f['area']} ({f['km']} km)",
                       {"lat": f["lat"], "lon": f["lon"]}))
        for g in s.get("rivers", {}).get("raised", []):
            ev.append(("river", f"river:{g['id']}:{g['state']}:{day}", f"River {g['river'] or g['name']} high",
                       f"{g['name']}: {g['level']} m, above its normal range ({g['km']} km away)",
                       {"lat": g["lat"], "lon": g["lon"]}))
        for o in s.get("sewage", {}).get("active", []):
            ev.append(("sewage", f"sewage:{o['id']}:{o['start']}", "Sewage discharge nearby",
                       f"{o['company']} storm overflow into {o['water']} ({o['km']} km)", {"lat": o["lat"], "lon": o["lon"]}))
        for f in s.get("fire", []):
            if _age_min(f.get("time")) < 180:
                ev.append(("fire", f"fire:{f.get('link') or f.get('title')}:{f.get('time')}", f"Fire service: {f['title']}",
                           f"{f.get('location') or ''} ({f['km']} km) · {f.get('service') or ''}", {"url": f.get("link")}))
        for e in s.get("roads", []):
            if e.get("kind") == "unplanned" and e.get("closure"):
                ev.append(("road", f"road:{e['id']}", f"Road closed: {', '.join(e.get('roads') or []) or e.get('type')}",
                           f"{e.get('where') or e.get('type')} ({e['km']} km)", {}))
        for c in s.get("power", []):
            if "unplanned" in str(c.get("type", "")).lower():
                ev.append(("power", f"power:{c['ref']}", "Power cut nearby",
                           f"{c['dno']}: {c.get('customers') or '?'} customers ({c['km']} km)", {}))
        for x in s.get("rnli", []):
            when = datetime.fromisoformat(x["time"]).astimezone(UK_TZ).strftime("%H:%M") if x.get("time") else "?"
            ev.append(("rnli", f"rnli:{x['id']}", f"RNLI {x['station']} lifeboat launched", f"{when}, {x['km']} km away", {}))
        for a in s.get("aircraft", {}).get("emergencies", []):
            ev.append(("aircraft", f"squawk:{a['hex']}:{a['squawk']}", f"Aircraft emergency: squawk {a['squawk']}",
                       f"{a.get('flight') or a['hex']} ({a['emergency']}) {a['km']} km away, {a.get('alt')} ft", {}))
        for h in s.get("aircraft", {}).get("hems", []):
            ev.append(("hems", f"hems:{h['hex']}:{h['first']}", "Air ambulance on scene nearby",
                       f"{h.get('flight') or h['hex']} landed {h['km']} km away", {}))
        lt = s.get("lightning", {})
        if lt.get("count_15min"):
            ev.append(("lightning", f"lightning:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')}", "Lightning nearby",
                       f"{lt['count_15min']} strikes in 15 min, nearest {lt['nearest_km']} km", {}))
        for p in s.get("iss", {}).get("passes", []):
            mins = -_age_min(p["start"])
            if 0 < mins <= 30:
                ev.append(("iss", f"iss:{p['start']}", "ISS visible soon",
                           f"Look up in {round(mins)} min: max {p['max_el']}° elevation", {}))
        rail = s.get("rail", {})
        for b in rail.get("boards", []):
            for t in b["services"]:
                if t.get("cancelled") or (t.get("late") or 0) >= 15:
                    what = "cancelled" if t.get("cancelled") else f"{t['late']} min late"
                    ev.append(("rail", f"rail:{t['id']}:{'cancelled' if t.get('cancelled') else 'late'}", f"{t['std']} {b['name']} to {t['dest']} {what}",
                               t.get("reason") or f"{t.get('operator') or ''}".strip(), {}))
        for d in rail.get("disruptions", []):
            if d.get("kind") == "Disruption":
                ev.append(("rail", f"raildis:{d['id']}", "Rail disruption near you", d["text"][:200], {"url": d.get("url")}))
        return [e for e in ev if e[0] in self.cfg.notify_kinds]

    async def notify_loop(self) -> None:
        cfg = self.cfg
        store = cfg.data_dir / "notified.json"
        try:
            sent: dict[str, float] = json.loads(store.read_text())
            first_run = False
        except (OSError, ValueError):
            sent, first_run = {}, True
        await asyncio.sleep(60)  # let the feeds warm up
        while True:
            try:
                events = self._home_events(await self.home_summary())
                now = time.time()
                new = [e for e in events if e[1] not in sent]
                if first_run:
                    # a fresh install starts from what's already happening rather than sending it all
                    _LOGGER.info("home notifications: baseline of %d current events, not sent", len(new))
                    new, first_run = [], False
                    sent.update({e[1]: now for e in events})
                for kind, key, title, message, extra in new[:5]:  # never burst more than 5 at once
                    payload = {"kind": kind, "title": title, "message": message, "home": cfg.home_name, **extra}
                    async with self.session.post(cfg.ha_webhook_url, json=payload,
                                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status >= 400:
                            raise RuntimeError(f"webhook HTTP {r.status}")
                    sent[key] = now
                    _LOGGER.info("notified: %s - %s", title, message)
                cutoff = now - SENT_KEEP_DAYS * 86400
                sent = {k: v for k, v in sent.items() if v > cutoff}
                await asyncio.to_thread(lambda: store.write_text(json.dumps(sent)))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("home notifications: %s", err)
            await asyncio.sleep(NOTIFY_EVERY_S)
