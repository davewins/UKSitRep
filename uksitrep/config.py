"""Runtime configuration, read from environment variables."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
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
                "source": c.get("source", "Curated"), "local": bool(c.get("local", True)),
            })
        return out
