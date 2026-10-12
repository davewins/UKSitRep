"""Aircraft type names and ICAO address -> country tables.

Both come from the tar1090 project (GPL) and are downloaded at runtime rather
than bundled, then cached in DATA_DIR.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
from pathlib import Path

import aiohttp

_LOGGER = logging.getLogger("uksitrep")

TYPES_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db/icao_aircraft_types2.js"
FLAGS_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090/master/html/flags.js"

# Friendlier / corrected names for common military types
TYPE_OVERRIDES = {
    "HAWK": "BAE Systems Hawk", "EUFI": "Eurofighter Typhoon", "LYNX": "AgustaWestland Lynx / Wildcat",
    "A332": "Airbus A330-200", "TEX2": "Beechcraft T-6 Texan II", "F35": "Lockheed Martin F-35 Lightning II",
    "A400": "Airbus A400M Atlas", "P8": "Boeing P-8 Poseidon", "E3TF": "Boeing E-3 Sentry (AWACS)",
    "E3CF": "Boeing E-3 Sentry (AWACS)", "K35R": "Boeing KC-135R Stratotanker",
    "C30J": "Lockheed Martin C-130J Hercules", "H47": "Boeing CH-47 Chinook", "H64": "Boeing AH-64 Apache",
    "PUMA": "Aérospatiale Puma", "EH10": "AgustaWestland AW101 Merlin", "RC35": "Boeing RC-135 Rivet Joint",
    "F15": "Boeing F-15 Eagle", "F16": "General Dynamics F-16 Fighting Falcon",
    "C17": "Boeing C-17 Globemaster III", "SHAD": "Beechcraft Shadow R1", "PRTR": "Shorts Protector",
}
HELI_TYPES = {"LYNX", "H47", "H64", "PUMA", "EH10"}


def _nice(s: str) -> str:
    return " ".join(w.title() if (w.isupper() and len(w) > 3 and w.isalpha()) else w for w in s.split())


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
        r.raise_for_status()
        return await r.read()


async def load(session: aiohttp.ClientSession, data_dir: Path) -> tuple[dict, list]:
    types_file, countries_file = data_dir / "aircraft_types.json", data_dir / "icao_countries.json"
    types: dict = {}
    countries: list = []
    try:
        raw = await _download(session, TYPES_URL)
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # already plain JSON
        types = {k: [_nice(v[0]), v[1]] for k, v in json.loads(raw).items()}
        for k, v in TYPE_OVERRIDES.items():
            types[k] = [v, "H2T" if k in HELI_TYPES else types.get(k, [None, "L2J"])[1]]
        flags = (await _download(session, FLAGS_URL)).decode()
        countries = sorted(
            [int(a, 16), int(b, 16), c, cc]
            for a, b, c, cc in re.findall(
                r'start:\s*0x([0-9A-Fa-f]+),\s*end:\s*0x([0-9A-Fa-f]+),\s*country:\s*"([^"]+)",\s*country_code:\s*"([a-z]*)"',
                flags,
            )
        )
        await asyncio.to_thread(types_file.write_text, json.dumps(types, separators=(",", ":")))
        await asyncio.to_thread(countries_file.write_text, json.dumps(countries, separators=(",", ":")))
        _LOGGER.info("lookup tables downloaded: %d types, %d country blocks", len(types), len(countries))
    except Exception as err:  # noqa: BLE001 - fall back to the cached copy
        _LOGGER.warning("lookup download failed (%s); using cached copy if present", err)
        try:
            types = json.loads(await asyncio.to_thread(types_file.read_text))
            countries = json.loads(await asyncio.to_thread(countries_file.read_text))
        except (OSError, ValueError):
            pass
    return types, countries
