"""Energy: the grid region by region, and what's generating where.

- Regions: the 14 distribution (DNO) areas, bundled from NESO's boundaries. Carbon Intensity
  (NESO), PV_Live (Sheffield Solar) and Octopus Agile all report by these same areas.
- Carbon Intensity gives each region's gCO2/kWh, its mix, and a 24 h forecast (for "cleanest time").
- PV_Live gives each region's estimated solar output against its installed capacity.
- Octopus publishes Agile half-hourly prices for each region a day ahead, keyless.
- Elexon's physical notifications (PN) give every Balancing Mechanism unit's planned output for
  now; units are mapped to sites by data/power_plants.json (tools/build_power_plants.py).
- Open Charge Map lists EV chargers (needs a free OCM_API_KEY).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import time

import aiohttp
from . import USER_AGENT

_LOGGER = logging.getLogger("uksitrep")

DATA = Path(__file__).with_name("data")
CI = "https://api.carbonintensity.org.uk"
PV_URL = "https://api.solar.sheffield.ac.uk/pvlive/api/v4/pes/{pes}"
OCTOPUS = "https://api.octopus.energy/v1"
PN_URL = "https://data.elexon.co.uk/bmrs/api/v1/datasets/PN/stream"
OCM_URL = "https://api.openchargemap.io/v3/poi/"
UA = {"User-Agent": USER_AGENT}
TIMEOUT = aiohttp.ClientTimeout(total=30)
# PES id (also PV_Live's) -> Carbon Intensity region id
CI_REGION = {17: 1, 18: 2, 16: 3, 15: 4, 23: 5, 13: 6, 21: 7, 14: 8, 11: 9, 10: 10, 22: 11, 20: 12, 12: 13, 19: 14}
CHARGER_MAX_SPAN = (0.25, 0.4)   # degrees lat, lon: about zoom 12 on a big screen
CI_FUELS = ("wind", "solar", "nuclear", "gas", "biomass", "hydro", "imports", "coal", "other")


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def _inside(lat: float, lon: float, ring: list) -> bool:
    c = False
    for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            c = not c
    return c


def best_window(rates: list[list], hours: float, key=lambda r: r[1]) -> tuple[str, str, float] | None:
    """Cheapest (or cleanest) run of `hours` from half-hourly [[start iso, value]] -> (start, end, mean)."""
    n = max(1, round(hours * 2))
    if len(rates) < n:
        return None
    best = min(range(len(rates) - n + 1), key=lambda i: sum(key(r) for r in rates[i:i + n]))
    end = datetime.fromisoformat(rates[best + n - 1][0].replace("Z", "+00:00")) + timedelta(minutes=30)
    return rates[best][0], end.isoformat(), round(sum(key(r) for r in rates[best:best + n]) / n, 2)


def runs_below(rates: list[list], limit: float) -> list[dict]:
    """Consecutive half-hours priced below `limit` -> [{start, end, min}]."""
    out: list[dict] = []
    for t, p in rates:
        end = (datetime.fromisoformat(t.replace("Z", "+00:00")) + timedelta(minutes=30)).isoformat()
        if p < limit:
            if out and out[-1]["end"] == datetime.fromisoformat(t.replace("Z", "+00:00")).isoformat():
                out[-1]["end"], out[-1]["min"] = end, min(out[-1]["min"], p)
            else:
                out.append({"start": datetime.fromisoformat(t.replace("Z", "+00:00")).isoformat(), "end": end, "min": p})
    return out


class Energy:
    """Mixin for Situation."""

    # ------------------------------------------------------------ bundled data
    def _energy_data(self) -> tuple[list[dict], list[dict]]:
        if not getattr(self, "_energy_static", None):
            regions = json.loads((DATA / "energy_regions.json").read_text())["regions"]
            plants = json.loads((DATA / "power_plants.json").read_text())["plants"]
            self._energy_static = (regions, plants)
        return self._energy_static

    def energy_region_at(self, lat: float, lon: float) -> dict | None:
        return next((r for r in self._energy_data()[0] if any(_inside(lat, lon, s) for s in r["shapes"])), None)

    async def energy_regions(self) -> dict:
        regions, _ = self._energy_data()
        return {"regions": regions}

    # ------------------------------------------------------------ Octopus Agile
    async def agile_product(self) -> str:
        async def fetch() -> dict:
            d = await self._json(f"{OCTOPUS}/products/", params={"brand": "OCTOPUS_ENERGY", "is_business": "false"},
                                 headers=UA, timeout=TIMEOUT)
            codes = sorted((p.get("available_from") or "", p["code"]) for p in d.get("results", [])
                           if p["code"].startswith("AGILE-") and "OUTGOING" not in p["code"])
            if not codes:
                raise RuntimeError("no Agile product listed")
            return {"code": codes[-1][1]}
        return (await self.cache.get("agile_product", 86400, fetch)).get("code") or "AGILE-24-10-01"

    async def agile_rates(self, letter: str) -> list[list]:
        """[[start iso, p/kWh inc VAT]] from the current half-hour, as far ahead as published."""
        async def fetch() -> dict:
            code = await self.agile_product()
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            start = now.replace(minute=0 if now.minute < 30 else 30)
            d = await self._json(f"{OCTOPUS}/products/{code}/electricity-tariffs/E-1R-{code}-{letter}/standard-unit-rates/",
                                 params={"period_from": _iso(start), "page_size": "200"}, headers=UA, timeout=TIMEOUT)
            rates = sorted([r["valid_from"].replace("+00:00", "Z"), round(r["value_inc_vat"], 2)]
                           for r in d.get("results", []) if r.get("valid_from"))
            return {"rates": rates}
        # new prices appear about 16:00; half-hourly refresh keeps "now" current
        hit = await self.cache.get(f"agile:{letter}", 1800, fetch)
        since = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")  # same shape as Octopus's
        return [r for r in hit.get("rates", []) if r[0] > since]

    # ------------------------------------------------------------ regions live
    async def energy_live(self) -> dict:
        async def fetch() -> dict:
            regions, _ = self._energy_data()

            async def pv(pes: int) -> tuple[int, dict]:
                try:
                    d = await self._json(PV_URL.format(pes=pes), params={"extra_fields": "capacity_mwp", "data_format": "json"},
                                         headers=UA, timeout=TIMEOUT)
                    _, t, mw, cap = d["data"][0]
                    return pes, {"mw": round(mw or 0), "cap": round(cap or 0), "at": t}
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("PV_Live %s: %s", pes, err)
                    return pes, {}

            async def agile(letter: str) -> tuple[str, list]:
                try:
                    return letter, await self.agile_rates(letter)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Agile %s: %s", letter, err)
                    return letter, []

            ci_task = asyncio.ensure_future(self._json(f"{CI}/regional", headers=UA, timeout=TIMEOUT))
            pvs = dict(await asyncio.gather(*(pv(r["pes"]) for r in regions)))
            ag = dict(await asyncio.gather(*(agile(r["letter"]) for r in regions)))
            try:
                ci = {x["regionid"]: x for x in (await ci_task)["data"][0]["regions"]}
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Carbon Intensity regional: %s", err)
                ci = {}
            out = []
            for r in regions:
                c = ci.get(CI_REGION[r["pes"]], {})
                mix = sorted(((m["fuel"], m["perc"]) for m in c.get("generationmix", []) if m.get("perc")), key=lambda m: -m[1])
                rates = ag.get(r["letter"], [])
                out.append({
                    "pes": r["pes"], "letter": r["letter"], "name": r["name"],
                    "carbon": {"g": c.get("intensity", {}).get("forecast"), "index": c.get("intensity", {}).get("index"),
                               "mix": [[f, round(p)] for f, p in mix[:5] if round(p)]} if c else None,
                    "solar": pvs.get(r["pes"]) or None,
                    "agile": {"now": rates[0][1] if rates else None, "rates": rates[:48]} if rates else None,
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "regions": out}

        return await self.cache.get("energy_live", 300, fetch)

    # ------------------------------------------------------------ generators
    async def generators(self) -> dict:
        async def fetch() -> dict:
            _, plants = self._energy_data()
            now = datetime.now(timezone.utc)
            rows = await self._json(PN_URL, params={"from": _iso(now - timedelta(minutes=2)), "to": _iso(now + timedelta(minutes=1))},
                                    headers=UA, timeout=aiohttp.ClientTimeout(total=60))
            level: dict[str, float] = {}
            for x in rows if isinstance(rows, list) else []:
                a, b = (datetime.fromisoformat(x[k].replace("Z", "+00:00")) for k in ("timeFrom", "timeTo"))
                if a <= now <= b:
                    f = (now - a) / (b - a) if b > a else 0
                    level[x["nationalGridBmUnit"]] = x["levelFrom"] + (x["levelTo"] - x["levelFrom"]) * f
            out = []
            for p in plants:
                got = [level[u] for u in p["units"] if u in level]
                if not got:
                    continue  # closed, mothballed, or not reporting
                mw = sum(got)
                out.append({k: p[k] for k in ("name", "fuel", "lat", "lon", "cap")} | {"mw": round(mw), **({"approx": True} if p.get("approx") else {})})
            if not out:
                raise RuntimeError("no physical notifications matched")
            return {"updated": now.isoformat(), "plants": out}

        return await self.cache.get("generators", 300, fetch)

    # ------------------------------------------------------------ EV chargers
    async def chargers(self, bbox: tuple[float, float, float, float] | None) -> dict:
        if not self.cfg.ocm_key:
            return {"error": "no_key", "note": "Add OCM_API_KEY (free from openchargemap.org) for EV chargers"}
        if not bbox:
            return {"error": "bbox required"}
        s, w, n, e = bbox
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 180) or n - s > CHARGER_MAX_SPAN[0] or e - w > CHARGER_MAX_SPAN[1]:
            return {"error": "zoom in", "chargers": []}
        s, w = [round((v // 0.05) * 0.05, 2) for v in (s, w)]
        n, e = [round(-((-v) // 0.05) * 0.05, 2) for v in (n, e)]

        async def fetch() -> dict:
            d = await self._json(OCM_URL, params={
                "output": "json", "boundingbox": f"({s},{w}),({n},{e})", "maxresults": "500",
                "compact": "false", "verbose": "false", "key": self.cfg.ocm_key}, headers=UA, timeout=TIMEOUT)
            out = []
            for p in d if isinstance(d, list) else []:
                a = p.get("AddressInfo") or {}
                if a.get("Latitude") is None:
                    continue
                conns = p.get("Connections") or []
                kw = max((c.get("PowerKW") or 0 for c in conns), default=0)
                kinds = sorted({((c.get("ConnectionType") or {}).get("Title") or "?").split(" (")[0] for c in conns})
                status = (p.get("StatusType") or {})
                out.append({
                    "id": p.get("ID"), "name": a.get("Title") or "Charger", "lat": round(a["Latitude"], 5), "lon": round(a["Longitude"], 5),
                    "address": ", ".join(x for x in (a.get("AddressLine1"), a.get("Town"), a.get("Postcode")) if x),
                    "operator": (p.get("OperatorInfo") or {}).get("Title"), "points": p.get("NumberOfPoints") or len(conns) or None,
                    "kw": round(kw) if kw else None, "connectors": kinds[:5],
                    "ok": status.get("IsOperational") is not False, "cost": (p.get("UsageCost") or "")[:80] or None,
                    "url": f"https://map.openchargemap.io/?id={p.get('ID')}",
                })
            return {"updated": datetime.now(timezone.utc).isoformat(), "chargers": out}

        return await self.cache.get(f"ocm:{s},{w},{n},{e}", 3600, fetch)

    # ------------------------------------------------------------ near home
    async def energy_home(self, h) -> dict:
        """The home's region: carbon now and the cleanest 3 h ahead, Agile now, cheapest 3 h, negative runs."""
        r = self.energy_region_at(h.lat, h.lon)
        if not r:
            return {}

        async def fetch() -> dict:
            now = datetime.now(timezone.utc)
            fw = await self._json(f"{CI}/regional/intensity/{_iso(now)}/fw24h/regionid/{CI_REGION[r['pes']]}",
                                  headers=UA, timeout=TIMEOUT)
            pts = [[x["from"], x["intensity"]["forecast"]] for x in fw.get("data", {}).get("data", [])
                   if x.get("intensity", {}).get("forecast") is not None]
            return {"forecast": pts}

        out: dict = {"region": r["name"], "letter": r["letter"]}
        live = await self.energy_live()
        reg = next((x for x in live.get("regions", []) if x["pes"] == r["pes"]), {})
        if reg.get("carbon"):
            out["carbon"] = reg["carbon"]
            try:
                fc = (await self.cache.get(f"cifw:{r['pes']}", 1800, fetch)).get("forecast", [])
                w = best_window(fc, 3)
                if w:
                    out["carbon"] = {**reg["carbon"], "clean": {"start": w[0], "end": w[1], "g": round(w[2])}}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("carbon forecast: %s", err)
        if reg.get("solar"):
            out["solar"] = reg["solar"]
        rates = (reg.get("agile") or {}).get("rates") or []
        if rates:
            w = best_window(rates, 3)
            out["agile"] = {"now": rates[0][1], "next": rates[1][1] if len(rates) > 1 else None,
                            "cheap": {"start": w[0], "end": w[1], "p": w[2]} if w else None,
                            "negative": runs_below(rates, 0)[:4], "until": rates[-1][0]}
        return out
