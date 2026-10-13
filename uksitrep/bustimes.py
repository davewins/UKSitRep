"""Bus stops and timetabled departures from the Bus Open Data Service's regional GTFS files.

There is no open "next buses" service, so the server downloads each chosen region's timetable
weekly (South West: 250 MB zip, 6 million stop times) and packs it into a small index:

- meta.json: stops, routes, headsigns, service calendars and one row per trip
- deps.bin:  every departure grouped by stop, 6 bytes each (minute of the service day, trip)

Building runs in a child process (python -m uksitrep.bustimes <zip> <dir>) so the server stays
responsive. A board is the stop's departures in the next three hours on today's (and, after
midnight, yesterday's) services, each marked live when a tracked bus is running that trip.
"""

from __future__ import annotations

import array
import asyncio
import csv
from datetime import date, datetime, timedelta, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
import struct
import sys
import time
import zipfile
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger("uksitrep")

GTFS_URL = "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/{region}/"
STOP_RE = re.compile(r"^[0-9A-Za-z]{4,20}$")
UK_TZ = ZoneInfo("Europe/London")
REFRESH_S = 7 * 86400
REC = struct.Struct("<HI")          # minute of the service day (may pass 1440), trip index
BOARD_AHEAD_MIN = 180
BOARD_ROWS = 12
MAX_STOPS = 800


# ------------------------------------------------------------------ build (child process)
def build(zip_path: str, out_dir: str) -> dict:
    z = zipfile.ZipFile(zip_path)

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))

    agencies = {r["agency_id"]: (r.get("agency_noc") or "", r["agency_name"]) for r in rows("agency.txt")}
    routes, route_idx = [], {}
    for r in rows("routes.txt"):
        noc, name = agencies.get(r["agency_id"], ("", ""))
        route_idx[r["route_id"]] = len(routes)
        routes.append([r["route_short_name"] or r["route_long_name"], noc, name])

    services, service_idx = [], {}
    for r in rows("calendar.txt"):
        service_idx[r["service_id"]] = len(services)
        mask = sum(1 << i for i, d in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
                   if r[d] == "1")
        services.append([mask, r["start_date"], r["end_date"], [], []])
    for r in rows("calendar_dates.txt"):
        if r["service_id"] not in service_idx:
            service_idx[r["service_id"]] = len(services)
            services.append([0, "99999999", "00000000", [], []])
        services[service_idx[r["service_id"]]][3 if r["exception_type"] == "1" else 4].append(r["date"])

    heads, head_idx = [], {}
    trip_idx: dict[str, int] = {}
    t_route, t_service, t_head = array.array("I"), array.array("I"), array.array("I")
    for r in rows("trips.txt"):
        h = r.get("trip_headsign") or ""
        if h not in head_idx:
            head_idx[h] = len(heads)
            heads.append(h)
        trip_idx[r["trip_id"]] = len(t_route)
        t_route.append(route_idx.get(r["route_id"], 0))
        t_service.append(service_idx.get(r["service_id"], 0))
        t_head.append(head_idx[h])
    t_start = array.array("H", [0xFFFF]) * len(t_route)

    stops, stop_idx = [], {}
    for r in rows("stops.txt"):
        try:
            lat, lon = round(float(r["stop_lat"]), 5), round(float(r["stop_lon"]), 5)
        except ValueError:
            continue
        stop_idx[r["stop_id"]] = len(stops)
        stops.append([r["stop_id"], r["stop_name"], lat, lon])

    per_stop: dict[int, array.array] = {}
    lines: dict[int, set] = {}

    def minute(t: str) -> int:
        h, m, _ = t.split(":")
        return int(h) * 60 + int(m)

    # stop_times come grouped by trip; a trip's last call is an arrival, not a departure
    pending: tuple | None = None
    for r in rows("stop_times.txt"):
        t = trip_idx.get(r["trip_id"])
        s = stop_idx.get(r["stop_id"])
        if t is None or s is None or not r["departure_time"]:
            continue
        m = minute(r["departure_time"])
        if t_start[t] == 0xFFFF or m < t_start[t]:
            t_start[t] = m
        if pending and pending[0] == t:
            ps, pm, pickup = pending[1], pending[2], pending[3]
            if pickup != "1":
                per_stop.setdefault(ps, array.array("I")).extend((pm, t))
                lines.setdefault(ps, set()).add(t_route[t])
        pending = (t, s, m, r.get("pickup_type") or "0")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    offsets, keep = {}, []
    with open(out / "deps.bin.tmp", "wb") as f:
        n = 0
        for s, arr in per_stop.items():
            recs = sorted(zip(arr[0::2], arr[1::2]))
            f.write(b"".join(REC.pack(m, t) for m, t in recs))
            offsets[s] = (n, len(recs))
            n += len(recs)
    for s, st in enumerate(stops):
        if s in offsets:  # stops with no departures are left off the map
            keep.append(st + [offsets[s][0], offsets[s][1], sorted({routes[r][0] for r in lines[s]})[:12]])
    meta = {
        "built": time.time(), "feed": next(rows("feed_info.txt"), {}).get("feed_version"),
        "stops": keep, "routes": routes, "heads": heads, "services": services,
        "trips": [list(t_route), list(t_service), list(t_head), list(t_start)],
    }
    (out / "meta.json.tmp").write_text(json.dumps(meta, separators=(",", ":")))
    os.replace(out / "deps.bin.tmp", out / "deps.bin")
    os.replace(out / "meta.json.tmp", out / "meta.json")
    return {"stops": len(keep), "trips": len(t_route), "departures": n}


def _active(svc: list, d: date) -> bool:
    ds = d.strftime("%Y%m%d")
    mask, start, end, added, removed = svc
    if ds in removed:
        return False
    return ds in added or (start <= ds <= end and bool(mask >> d.weekday() & 1))


class _Region:
    def __init__(self, name: str, path: Path):
        self.name, self.path = name, path
        meta = json.loads((path / "meta.json").read_text())
        self.built = meta["built"]
        self.routes, self.heads, self.services = meta["routes"], meta["heads"], meta["services"]
        self.t_route, self.t_service, self.t_head, self.t_start = meta["trips"]
        self.stops = {s[0]: s for s in meta["stops"]}

    def departures(self, stop: str, now: datetime) -> list[dict]:
        st = self.stops.get(stop)
        if not st:
            return []
        off, count = st[4], st[5]
        with open(self.path / "deps.bin", "rb") as f:
            f.seek(off * REC.size)
            raw = f.read(count * REC.size)
        today = now.date()
        nm = now.hour * 60 + now.minute
        days = [(today, 0), (today - timedelta(days=1), 1440)]
        active = {}
        out = []
        for m, t in REC.iter_unpack(raw):
            for d, shift in days:
                mm = m - shift
                if not (nm - 1 <= mm <= nm + BOARD_AHEAD_MIN):
                    continue
                svc = self.t_service[t]
                if (svc, d) not in active:
                    active[(svc, d)] = _active(self.services[svc], d)
                if active[(svc, d)]:
                    line, noc, op = self.routes[self.t_route[t]]
                    when = datetime.combine(d, datetime.min.time(), UK_TZ) + timedelta(minutes=m)
                    start = datetime.combine(d, datetime.min.time(), UK_TZ) + timedelta(minutes=self.t_start[t])
                    out.append({"time": when.isoformat(), "line": line, "dest": self.heads[self.t_head[t]],
                                "operator": op, "noc": noc, "start": start.strftime("%H:%M")})
        return out


class BusTimes:
    """Mixin for Situation."""

    def _bt_regions(self) -> dict[str, _Region]:
        loaded = getattr(self, "_bt_loaded", None)
        if loaded is None:
            loaded = self._bt_loaded = {}
        root = self.cfg.data_dir / "bustimes"
        for name in self.cfg.bus_timetable_regions:
            meta = root / name / "meta.json"
            try:
                mtime = meta.stat().st_mtime
            except OSError:
                continue
            r = loaded.get(name)
            if not r or r.mtime != mtime:
                try:
                    r = _Region(name, root / name)
                    r.mtime = mtime
                    loaded[name] = r
                    _LOGGER.info("Bus timetable %s: %d stops", name, len(r.stops))
                except (OSError, ValueError, KeyError) as err:
                    _LOGGER.warning("Bus timetable %s unreadable: %s", name, err)
        return loaded

    def bustimes_maintain(self) -> None:
        """Start a background refresh for any region that is missing or a week old."""
        if time.time() < self.bt_retry_at or (self.bt_task and not self.bt_task.done()):
            return
        regions = self._bt_regions()
        due = [n for n in self.cfg.bus_timetable_regions
               if n not in regions or time.time() - regions[n].built > REFRESH_S]
        if due:
            self.bt_task = self.spawn(self._bt_refresh(due))

    async def _bt_refresh(self, names: list[str]) -> None:
        root = self.cfg.data_dir / "bustimes"
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            zpath = root / f"{name}.zip"
            try:
                async with self.session.get(GTFS_URL.format(region=name), timeout=aiohttp.ClientTimeout(total=1800)) as r:
                    r.raise_for_status()
                    with open(zpath, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 20):
                            f.write(chunk)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "uksitrep.bustimes", str(zpath), str(root / name),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=str(Path(__file__).parent.parent))
                out, err = await proc.communicate()
                if proc.returncode:
                    raise RuntimeError(err.decode(errors="replace").strip().splitlines()[-1:] or "build failed")
                _LOGGER.info("Bus timetable %s rebuilt: %s", name, out.decode().strip())
            except Exception as err:  # noqa: BLE001
                self.bt_retry_at = time.time() + 6 * 3600
                _LOGGER.warning("Bus timetable %s refresh failed (%s); retrying in 6 h", name, err)
            finally:
                zpath.unlink(missing_ok=True)

    async def bus_stops(self, bbox: tuple[float, float, float, float] | None) -> dict:
        if not self.cfg.bus_timetable_regions:
            return {"error": "not set: BUS_TIMETABLE_REGIONS", "note": "disabled: set BUS_TIMETABLE_REGIONS (e.g. south_west)"}
        self.bustimes_maintain()
        regions = self._bt_regions()
        if not regions:
            return {"error": "loading", "note": "Bus timetables are downloading (first start takes a few minutes)", "stops": []}
        if not bbox:
            return {"error": "bbox required"}
        s, w, n, e = bbox
        if n - s > 0.2 or e - w > 0.3:
            return {"error": "zoom in", "stops": []}
        seen, out = set(), []
        for r in regions.values():
            for st in r.stops.values():
                if s <= st[2] <= n and w <= st[3] <= e and st[0] not in seen:
                    seen.add(st[0])
                    out.append({"id": st[0], "name": st[1], "lat": st[2], "lon": st[3], "lines": st[6]})
                    if len(out) >= MAX_STOPS:
                        break
        return {"stops": out}

    async def bus_board(self, stop: str) -> dict:
        if not STOP_RE.match(stop or ""):
            return {"error": "bad stop"}
        regions = self._bt_regions()
        hits = [r for r in regions.values() if stop in r.stops]
        if not hits:
            return {"error": "unknown stop"}

        async def fetch() -> dict:
            now = datetime.now(UK_TZ)
            deps, seen = [], set()
            for r in hits:
                for d in await asyncio.to_thread(r.departures, stop, now):
                    k = (d["time"], d["line"], d["dest"])
                    if k not in seen:
                        seen.add(k)
                        deps.append(d)
            deps.sort(key=lambda d: d["time"])
            deps = deps[:BOARD_ROWS]
            st = hits[0].stops[stop]
            await self._bt_live(deps, st[2], st[3])
            for d in deps:
                d.pop("noc", None)
                d.pop("start", None)
            return {"id": stop, "name": st[1], "lat": st[2], "lon": st[3], "departures": deps,
                    "generated": datetime.now(timezone.utc).isoformat()}

        return await self.cache.get(f"busboard:{stop}", 60, fetch)

    async def _bt_live(self, deps: list[dict], lat: float, lon: float) -> None:
        """Mark departures whose trip a tracked bus is running (same operator, line and start time)."""
        if not self.cfg.bods_key or not deps:
            return
        try:
            live = await self.buses((lat - 0.12, lon - 0.2, lat + 0.12, lon + 0.2))
        except Exception:  # noqa: BLE001 - the board is useful without it
            return
        from .home import _km
        index = {}
        for b in live.get("buses", []):
            if b.get("dep"):
                try:
                    start = datetime.fromisoformat(b["dep"]).astimezone(UK_TZ).strftime("%H:%M")
                except ValueError:
                    continue
                index[(b["op"], b["line"].lower(), start)] = b
        for d in deps:
            b = index.get((d["noc"], d["line"].lower(), d["start"]))
            if b:
                d["live"] = {"km": round(_km(lat, lon, b["lat"], b["lon"]), 1), "at": b["at"]}

    async def home_bus_stop(self, h) -> dict | None:
        """The nearest stop to a home (within 500 m) that has departures, with its board."""
        regions = self._bt_regions()
        if not regions:
            return None
        from .home import _km

        async def nearest() -> dict:
            best = min(((_km(h.lat, h.lon, s[2], s[3]), s[0]) for r in regions.values() for s in r.stops.values()),
                       default=(99, None))
            return {"stop": best[1] if best[0] <= 0.5 else None}

        stop = (await self.cache.get(f"bushome:{h.key}:{len(regions)}", 3600, nearest))["stop"]
        if not stop:
            return None
        board = await self.bus_board(stop)
        return board if not board.get("error") else None


if __name__ == "__main__":
    print(json.dumps(build(sys.argv[1], sys.argv[2])))
