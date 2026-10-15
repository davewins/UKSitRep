"""Runtime configuration, read from environment variables."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import re
from pathlib import Path

_LOGGER = logging.getLogger("uksitrep")

PACKAGE_DIR = Path(__file__).parent

# Devon & Somerset FRS incident-page station filter ids (Teignbridge + Torbay)
DEFAULT_DSFRS_STATIONS = {
    "280": "Ashburton", "279": "Bovey Tracey", "281": "Brixham", "278": "Buckfastleigh",
    "277": "Dawlish", "276": "Moretonhampstead", "275": "Newton Abbot", "282": "Paignton",
    "274": "Teignmouth", "283": "Torquay",
}


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


DEFAULT_NOTIFY = "warning,flood,river,sewage,fire,road,power,rnli,aircraft,hems,lightning,iss,rail,bus,agile"
MAX_HOMES = 9
BUS_REGIONS = ("north_east", "north_west", "yorkshire", "east_midlands", "west_midlands", "east_anglia",
               "south_east", "south_west", "london")  # the BODS GTFS regions


def _bus_regions() -> list[str]:
    """BUS_TIMETABLE_REGIONS, forgiving about case, spaces, hyphens and quotes."""
    out = []
    for raw in _list("BUS_TIMETABLE_REGIONS"):
        r = re.sub(r"[\s-]+", "_", raw.strip("'\" ").lower())
        if r in BUS_REGIONS:
            out.append(r)
        else:
            _LOGGER.warning("BUS_TIMETABLE_REGIONS: unknown region %r (use one of %s)", raw, ", ".join(BUS_REGIONS))
    return out


@dataclass
class Home:
    """A place the Near home panel and notifications are about. One deployment can serve several:
    HOME_* is the first, HOME2_* ... HOME9_* the others. A home's own token picks it by default."""
    key: str
    name: str
    lat: float
    lon: float
    radius_km: float = 25.0
    webhook_url: str | None = None
    notify_kinds: set[str] = field(default_factory=set)
    rail_stations: list[str] = field(default_factory=list)
    token: str | None = None


def _homes() -> list[Home]:
    homes: list[Home] = []
    for n in range(1, MAX_HOMES + 1):
        pre = "HOME" if n == 1 else f"HOME{n}"
        try:
            lat, lon = float(os.environ[f"{pre}_LAT"]), float(os.environ[f"{pre}_LON"])
        except (KeyError, ValueError):
            continue
        name = os.environ.get(f"{pre}_NAME") or ("Home" if n == 1 else f"Home {n}")
        key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or str(n)
        if any(h.key == key for h in homes):
            key = f"{key}-{n}"
        # home 1 keeps its original variable names
        hook, notify, rail = (("HA_WEBHOOK_URL", "NOTIFY", "RAIL_STATIONS") if n == 1 else
                              (f"{pre}_WEBHOOK_URL", f"{pre}_NOTIFY", f"{pre}_RAIL_STATIONS"))
        homes.append(Home(
            key=key, name=name, lat=lat, lon=lon,
            radius_km=float(os.environ.get(f"{pre}_RADIUS_KM") or 25),
            webhook_url=os.environ.get(hook) or None,
            notify_kinds=set(_list(notify, os.environ.get("NOTIFY") or DEFAULT_NOTIFY)),
            rail_stations=[c.upper() for c in _list(rail) if re.fullmatch(r"[A-Za-z]{3}", c)],
            token=(os.environ.get("ACCESS_TOKEN") if n == 1 else os.environ.get(f"{pre}_TOKEN")) or None,
        ))
    return homes


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: Path = Path("/data")
    access_token: str | None = None
    ais_key: str | None = None
    nh_keys: list[str] = field(default_factory=list)
    enable_worldcam: bool = False
    enable_background_adsb: bool = True
    dsfrs_stations: dict[str, str] = field(default_factory=dict)
    dsfrs_areas: set[str] = field(default_factory=set)
    bbc_local_feeds: dict[str, str] = field(default_factory=dict)
    cameras_file: Path | None = None
    firms_key: str | None = None
    enable_lightning: bool = True
    fuel_client_id: str | None = None
    fire_services: set[str] = field(default_factory=set)
    homes: list[Home] = field(default_factory=list)
    fuel_client_secret: str | None = None
    rail_ldb_key: str | None = None
    rail_disruptions_key: str | None = None
    bods_key: str | None = None
    ocm_key: str | None = None
    bus_timetable_regions: list[str] = field(default_factory=list)

    def home(self, key: str | None = None) -> Home | None:
        """The home with this key, else the first."""
        return next((h for h in self.homes if h.key == key), self.homes[0] if self.homes else None)

    def tokens(self) -> dict[str, str | None]:
        """Every accepted access token -> the home it opens on (None: the first)."""
        out: dict[str, str | None] = {self.access_token: None} if self.access_token else {}
        out.update({h.token: h.key for h in self.homes if h.token})
        return out

    @classmethod
    def from_env(cls) -> "Config":
        dsfrs = {}
        if _bool("ENABLE_DSFRS", False):
            ids = _list("DSFRS_STATION_IDS")
            dsfrs = {i: DEFAULT_DSFRS_STATIONS.get(i, i) for i in ids} if ids else dict(DEFAULT_DSFRS_STATIONS)
        bbc = {
            r.replace("_", " ").title(): f"https://feeds.bbci.co.uk/news/england/{r}/rss.xml"
            for r in _list("BBC_LOCAL_REGIONS")
        }
        cams = os.environ.get("CAMERAS_FILE")
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8080")),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            access_token=os.environ.get("ACCESS_TOKEN") or None,
            ais_key=os.environ.get("AISSTREAM_API_KEY") or None,
            nh_keys=[k for k in (os.environ.get("NATIONAL_HIGHWAYS_KEY"),
                                 os.environ.get("NATIONAL_HIGHWAYS_KEY_SECONDARY")) if k],
            enable_worldcam=_bool("ENABLE_WORLDCAM", False),
            enable_background_adsb=_bool("ENABLE_BACKGROUND_ADSB", True),
            dsfrs_stations=dsfrs,
            dsfrs_areas=set(_list("DSFRS_AREAS", "Teignbridge,Torbay")),
            bbc_local_feeds=bbc,
            cameras_file=Path(cams) if cams else None,
            firms_key=os.environ.get("FIRMS_MAP_KEY") or None,
            enable_lightning=_bool("ENABLE_LIGHTNING", True),
            fuel_client_id=os.environ.get("FUEL_FINDER_CLIENT_ID") or None,
            fire_services=set(_list("FIRE_SERVICES", "avon,bucks,cheshire,cleveland,hants,rbfrs,shropshire")),
            homes=_homes(),
            fuel_client_secret=os.environ.get("FUEL_FINDER_CLIENT_SECRET") or None,
            rail_ldb_key=os.environ.get("RAIL_LDB_KEY") or None,
            rail_disruptions_key=os.environ.get("RAIL_DISRUPTIONS_KEY") or None,
            bods_key=os.environ.get("BODS_API_KEY") or None,
            ocm_key=os.environ.get("OCM_API_KEY") or None,
            bus_timetable_regions=_bus_regions(),
        )

    def curated_cams(self) -> list[dict]:
        """Hand-curated webcams from CAMERAS_FILE (or the bundled default)."""
        path = self.cameras_file or PACKAGE_DIR.parent / "config" / "cameras.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as err:
            _LOGGER.warning("cameras file %s not loaded: %s", path, err)
            return []
        out = []
        for i, c in enumerate(data.get("cameras", [])):
            if not all(k in c for k in ("name", "lat", "lon", "url")):
                continue
            out.append({
                "id": f"cur{i}", "name": c["name"], "lat": c["lat"], "lon": c["lon"],
                "url": c["url"], "image": c.get("image"), "video": c.get("video"),
                "jw": c.get("jw"), "stream": c.get("stream"), "embed": c.get("embed"),
                "source": c.get("source", "Curated"), "local": bool(c.get("local", True)),
            })
        return out
