"""Buses: live positions and disruptions from the DfT Bus Open Data Service (BODS), with
operator names from Traveline's National Operator Codes (NOC).

- SIRI-VM gives every bus's last GPS fix for a bounding box. The whole of England is 30 MB,
  so the map asks only for what's in view, once zoomed in.
- SIRI-SX gives operators' disruption notices (diversions, closed stops) with the stops affected.
- The NOC list turns "SDVN" into "Stagecoach South West"; a small extract is bundled and
  refreshed monthly in the background.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

import aiohttp
from . import USER_AGENT

_LOGGER = logging.getLogger("uksitrep")

BODS = "https://data.bus-data.dft.gov.uk/api/v1"
VM_URL = BODS + "/datafeed/"
SX_URL = BODS + "/siri-sx/"
NOC_URL = "https://www.travelinedata.org.uk/noc/api/1.0/nocrecords.xml"
SIRI = "{http://www.siri.org.uk/siri}"
BUS_TIMEOUT = aiohttp.ClientTimeout(total=30)
BUS_MAX_AGE_S = 600          # older fixes are buses that have finished for the day
BUS_MAX_SPAN = (1.0, 1.6)    # degrees lat, lon: a big screen at zoom 12; England is 30 MB
OPS_REFRESH_S = 30 * 86400


def _tidy(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("_", " ")).strip()


def _when(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def parse_noc(xml: bytes) -> dict[str, list]:
    """NOC records -> {code: [public name, website or None]} (operators still trading)."""
    root = ET.fromstring(xml.decode("utf-8", "replace"))  # declares UTF-8 but has the odd cp1252 byte
    sites = {}
    for p in root.iterfind("PublicName/PublicNameRecord"):
        site = (p.findtext("Website") or "").strip("# ")
        sites[p.findtext("PubNmId")] = site if site.startswith("http") else None
    out = {}
    for r in root.iterfind("NOCTable/NOCTableRecord"):
        code, name = (r.findtext("NOCCODE") or "").strip(), (r.findtext("OperatorPublicName") or "").strip()
        if code and name and not (r.findtext("DateCeased") or "").strip():
            out[code] = [name, sites.get(r.findtext("PubNmId"))]
    return out


def parse_vm(xml: bytes, now: float | None = None) -> list[dict]:
    """SIRI-VM -> one entry per vehicle (latest fix), dropping fixes older than BUS_MAX_AGE_S."""
    now = now or time.time()
    out: dict[str, dict] = {}
    for _, el in ET.iterparse(io.BytesIO(xml), events=("end",)):
        if el.tag != SIRI + "VehicleActivity":
            continue
        j = el.find(SIRI + "MonitoredVehicleJourney")
        at = _when(el.findtext(SIRI + "RecordedAtTime"))
        lat = j.findtext(f"{SIRI}VehicleLocation/{SIRI}Latitude") if j is not None else None
        lon = j.findtext(f"{SIRI}VehicleLocation/{SIRI}Longitude") if j is not None else None
        if j is None or not at or not lat or not lon or now - at.timestamp() > BUS_MAX_AGE_S:
            el.clear()
            continue
        op = j.findtext(SIRI + "OperatorRef") or ""
        vid = f"{op}:{j.findtext(SIRI + 'VehicleRef') or el.findtext(SIRI + 'ItemIdentifier')}"
        if vid in out and out[vid]["at"] >= at.isoformat():
            el.clear()
            continue
        try:
            bearing = round(float(j.findtext(SIRI + "Bearing")))
        except (TypeError, ValueError):
            bearing = None
        out[vid] = {
            "id": vid, "line": _tidy(j.findtext(SIRI + "PublishedLineName") or j.findtext(SIRI + "LineRef")),
            "op": op, "from": _tidy(j.findtext(SIRI + "OriginName")), "dest": _tidy(j.findtext(SIRI + "DestinationName")),
            "dep": j.findtext(SIRI + "OriginAimedDepartureTime"), "vehicle": _tidy(j.findtext(SIRI + "VehicleRef")),
            "lat": round(float(lat), 5), "lon": round(float(lon), 5), "bearing": bearing, "at": at.isoformat(),
        }
        el.clear()
    return list(out.values())


def parse_sx(xml: bytes, now: datetime | None = None) -> list[dict]:
    """SIRI-SX -> current, open situations that name at least one stop."""
    now = now or datetime.now(timezone.utc)
    root = ET.fromstring(xml)
    out = []
    for s in root.iter(SIRI + "PtSituationElement"):
        if (s.findtext(SIRI + "Progress") or "open") == "closed":
            continue
        periods = [(_when(v.findtext(SIRI + "StartTime")), _when(v.findtext(SIRI + "EndTime")))
                   for v in s.findall(SIRI + "ValidityPeriod")]
        cur = [(a, b) for a, b in periods if (not a or a <= now) and (not b or b >= now)]
        if periods and not cur:
            continue
        stops, seen = [], set()
        for p in s.iter(SIRI + "AffectedStopPoint"):
            ref, la, lo = p.findtext(SIRI + "StopPointRef"), p.findtext(f".//{SIRI}Latitude"), p.findtext(f".//{SIRI}Longitude")
            if la and lo and ref not in seen:
                seen.add(ref)
                stops.append({"name": _tidy(p.findtext(SIRI + "StopPointName")), "lat": round(float(la), 5), "lon": round(float(lo), 5)})
        if not stops:
            continue
        lines, lseen = [], set()
        for a in s.iter(SIRI + "AffectedLine"):
            k = (a.findtext(f"{SIRI}AffectedOperator/{SIRI}OperatorName") or "", _tidy(a.findtext(SIRI + "PublishedLineName")))
            if k[1] and k not in lseen:
                lseen.add(k)
                lines.append({"op": k[0], "line": k[1]})
        reason = next((_tidy(c.text) for c in s if c.tag.endswith("Reason") and c.text), None)
        sev = [c.text for c in s.iter(SIRI + "Severity") if c.text]
        end = max((b for _, b in cur if b), default=None) if cur and all(b for _, b in cur) else None
        out.append({
            "id": s.findtext(SIRI + "SituationNumber"), "summary": _tidy(s.findtext(SIRI + "Summary")),
            "text": _tidy(s.findtext(SIRI + "Description"))[:500], "reason": reason, "severity": sev[0] if sev else None,
            "planned": (s.findtext(SIRI + "Planned") or "").lower() == "true",
            "url": next((u.text for u in s.iter(SIRI + "Uri") if (u.text or "").startswith("http")), None),
            "end": end.isoformat() if end else None, "lines": lines[:12], "stops": stops[:30],
        })
    out.sort(key=lambda x: (x["planned"], x["summary"]))
    return out


class Bus:
    """Mixin for Situation."""

    # ------------------------------------------------------------ operators
    async def bus_operators(self) -> dict[str, list]:
        if getattr(self, "_noc", None) and time.time() - self._noc[0] < 3600:
            return self._noc[1]
        store = self.cfg.data_dir / "bus_operators.json"
        saved: dict = {"fetched": 0, "operators": {}}
        for path in (store, Path(__file__).with_name("data") / "bus_operators.json"):
            try:
                saved = await asyncio.to_thread(lambda: json.loads(path.read_text()))
                break
            except (OSError, ValueError):
                continue
        due = time.time() - saved.get("fetched", 0) > OPS_REFRESH_S and time.time() > self.noc_retry_at
        if due and (not self.noc_task or self.noc_task.done()):
            self.noc_task = self.spawn(self._refresh_bus_operators(store))
        self._noc = (time.time(), saved.get("operators", {}))
        return self._noc[1]

    async def _refresh_bus_operators(self, store: Path) -> None:
        try:
            async with self.session.get(NOC_URL, timeout=aiohttp.ClientTimeout(total=180)) as r:
                r.raise_for_status()
                raw = await r.read()
            ops = await asyncio.to_thread(parse_noc, raw)
            if len(ops) < 1000:
                raise RuntimeError(f"only {len(ops)} operators")
            await asyncio.to_thread(lambda: store.write_text(json.dumps({"fetched": time.time(), "operators": ops},
                                                                        separators=(",", ":"))))
            self._noc = None
            _LOGGER.info("Bus operator names refreshed: %d", len(ops))
        except Exception as err:  # noqa: BLE001
            self.noc_retry_at = time.time() + 6 * 3600
            _LOGGER.warning("Bus operator names refresh failed (%s); retrying in 6 h", err)

    # ------------------------------------------------------------ live positions
    async def buses(self, bbox: tuple[float, float, float, float] | None) -> dict:
        """Live buses in (south, west, north, east)."""
        if not self.cfg.bods_key:
            return {"error": "no_key", "note": "Add BODS_API_KEY for live buses"}
        if not bbox:
            return {"error": "bbox required"}
        s, w, n, e = bbox
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 180) or n - s > BUS_MAX_SPAN[0] or e - w > BUS_MAX_SPAN[1]:
            return {"error": "zoom in", "buses": []}
        # snap outwards to a 0.02° grid so nearby views share a cache entry
        s, w = [round((v // 0.02) * 0.02, 2) for v in (s, w)]
        n, e = [round(-((-v) // 0.02) * 0.02, 2) for v in (n, e)]

        async def fetch() -> dict:
            async with self.session.get(VM_URL, params={"boundingBox": f"{w},{s},{e},{n}", "api_key": self.cfg.bods_key},
                                        headers={"User-Agent": USER_AGENT}, timeout=BUS_TIMEOUT) as r:
                r.raise_for_status()
                raw = await r.read()
            buses = await asyncio.to_thread(parse_vm, raw)
            ops = await self.bus_operators()
            for b in buses:
                o = ops.get(b["op"])
                b["operator"], b["site"] = (o[0], o[1]) if o else (b["op"], None)
            return {"updated": datetime.now(timezone.utc).isoformat(), "buses": buses}

        return await self.cache.get(f"bus:{s},{w},{n},{e}", 15, fetch)

    # ------------------------------------------------------------ disruptions
    async def bus_disruptions(self) -> dict:
        if not self.cfg.bods_key:
            return {"error": "no_key", "note": "Add BODS_API_KEY for bus disruptions"}

        async def fetch() -> dict:
            async with self.session.get(SX_URL, params={"api_key": self.cfg.bods_key}, headers={"User-Agent": USER_AGENT},
                                        timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                raw = await r.read()
            items = await asyncio.to_thread(parse_sx, raw)
            return {"updated": datetime.now(timezone.utc).isoformat(), "disruptions": items}

        return await self.cache.get("bus_disruptions", 300, fetch)
