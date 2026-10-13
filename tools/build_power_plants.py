"""Build uksitrep/data/power_plants.json: transmission-connected generators with a position and
their Balancing Mechanism units, so live output (Elexon physical notifications) can be mapped.

    python3 tools/build_power_plants.py

Positions and BMU ids: OSUKED Power Station Dictionary (MIT). Fuel and capacity: Elexon BMRS
BM unit reference. Farms and plants built since the dictionary's last update are in EXTRA below,
placed by hand at their approximate centre (offshore wind farms cover tens of km anyway).
"""

import csv
import io
import json
from pathlib import Path
import re
import urllib.request

OSUKED = "https://raw.githubusercontent.com/OSUKED/Power-Station-Dictionary/shiro/data/"
BMUS = "https://data.elexon.co.uk/bmrs/api/v1/reference/bmunits/all"
FUELS = {"WIND", "CCGT", "OCGT", "NUCLEAR", "BIOMASS", "PS", "NPSHYD", "OIL"}  # no coal: the last plant shut in 2024

# BMU prefix -> (name, lat, lon), approximate
EXTRA = {
    "SOFWO": ("Sofia (Dogger Bank)", 54.98, 2.15), "SGRWO": ("Seagreen", 56.58, -1.75),
    "HOWBO": ("Hornsea 2", 53.93, 1.62), "DBAWO": ("Dogger Bank A", 55.05, 1.85), "DBBWO": ("Dogger Bank B", 55.20, 2.20),
    "INCWO": ("Inch Cape", 56.48, -2.25), "KEAD": ("Keadby 2", 53.596, -0.756), "MOWWO": ("Moray West", 58.10, -3.10),
    "VKNGW": ("Viking (Shetland)", 60.30, -1.25), "NNGAO": ("Neart na Gaoithe", 56.27, -2.32),
    "SOKYW": ("South Kyle", 55.32, -4.33), "SEEL": ("Spalding Energy Expansion", 52.81, -0.14),
    "HIRWN": ("Hirwaun", 51.75, -3.52), "TSREP": ("Teesside Renewable Energy Plant", 54.625, -1.16),
    "KLGLW": ("Kilgallioch", 55.03, -4.75), "NOKYW": ("North Kyle", 55.38, -4.37), "WILCT": ("Wilton", 54.585, -1.115),
    "CUMHW": ("Cumberhead", 55.57, -3.95), "LIMKW": ("Limekiln", 58.53, -3.71), "CREAW": ("Creag Riabhach", 58.13, -4.40),
    "SAKNW": ("Sandy Knowe", 55.43, -3.98), "PLOEW": ("Pencloe", 55.34, -4.13),
}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "UKSitRep build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> None:
    units = {b["nationalGridBmUnit"]: b for b in json.loads(get(BMUS)) if b.get("fuelType") in FUELS}
    ids = list(csv.DictReader(io.StringIO(get(OSUKED + "dictionary/ids.csv").decode())))
    pos = {r["dictionary_id"]: (float(r["latitude"]), float(r["longitude"]))
           for r in csv.DictReader(io.StringIO(get(OSUKED + "attribute_sources/plant-locations/plant-locations.csv").decode()))}
    plants, used = [], set()

    def add(name, lat, lon, bmus, approx=False):
        live = [u for u in bmus if u in units and u not in used]
        cap = sum(float(units[u]["generationCapacity"] or 0) for u in live)
        if not live or cap < 5:
            return
        used.update(live)
        by_fuel: dict[str, float] = {}
        for u in live:
            by_fuel[units[u]["fuelType"]] = by_fuel.get(units[u]["fuelType"], 0) + float(units[u]["generationCapacity"] or 0)
        name = re.sub(r"(\s+(power station|generator|wind ?farm|offshore wind farm))?[\s-]*$", "", name.strip(), flags=re.I) or name
        plants.append({"name": name, "fuel": max(by_fuel, key=by_fuel.get), "lat": round(lat, 4), "lon": round(lon, 4),
                       "cap": round(cap), "units": sorted(live), **({"approx": True} if approx else {})})

    for r in ids:
        if r["dictionary_id"] in pos:
            add(r["name"], *pos[r["dictionary_id"]], [x.strip() for x in (r["ngc_bmu_id"] or "").split(",")])
    for prefix, (name, lat, lon) in EXTRA.items():
        add(name, lat, lon, [u for u in units if re.fullmatch(rf"{prefix}[-_]?\w*", u)], approx=True)
    plants.sort(key=lambda p: -p["cap"])
    dest = Path(__file__).resolve().parents[1] / "uksitrep" / "data" / "power_plants.json"
    dest.write_text(json.dumps({"source": "OSUKED Power Station Dictionary (MIT); Elexon BMRS", "plants": plants},
                               separators=(",", ":")))
    tot = sum(float(u["generationCapacity"] or 0) for u in units.values())
    print(dest, len(plants), "plants,", f"{sum(p['cap'] for p in plants) / tot:.0%} of BM capacity mapped")


if __name__ == "__main__":
    main()
