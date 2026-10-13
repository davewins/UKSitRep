"""Build uksitrep/data/areas.json: UK nations, English regions, counties and unitary authorities,
local authority districts and national parks, with simplified outlines, for the place search.

    python3 tools/build_areas.py

Boundaries: ONS Open Geography Portal, "BUC" (ultra generalised, clipped to the coastline) sets,
fetched already simplified to about 400 m. Open Government Licence; contains OS data © Crown copyright.
"""

import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

ONS = "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/{}/FeatureServer/0/query"
SETS = [  # (service, field prefix)
    ("Countries_December_2025_Boundaries_UK_BUC", "CTRY25"),
    ("Regions_December_2025_Boundaries_EN_BUC", "RGN25"),
    ("Counties_and_Unitary_Authorities_December_2025_Boundaries_UK_BUC", "CTYUA25"),
    ("Local_Authority_Districts_December_2024_Boundaries_UK_BUC", "LAD24"),
    ("National_Parks_December_2022_Boundaries_GB_BUC", "NPARK22"),
]
KIND = {"E92": "Nation", "W92": "Nation", "S92": "Nation", "N92": "Nation", "E12": "Region",
        "E10": "County", "E06": "Unitary authority", "E08": "Metropolitan borough", "E09": "London borough",
        "W06": "Welsh county", "S12": "Council area", "N09": "District council", "E07": "District"}
NATION = {"W": "Wales", "S": "Scotland", "N": "Northern Ireland"}
MIN_ISLAND = 0.002  # deg², about 15 km²: smaller islands are dropped (an area's largest ring is always kept)


def get(service: str) -> dict:
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson",
                                "maxAllowableOffset": "0.004", "geometryPrecision": "3"})
    req = urllib.request.Request(ONS.format(service) + "?" + q, headers={"User-Agent": "UKSitRep build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def ring_area(r: list) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(r, r[1:] + r[:1]))) / 2


def inside(lat: float, lon: float, ring: list) -> bool:
    hit, j = False, len(ring) - 1
    for i in range(len(ring)):
        (yi, xi), (yj, xj) = ring[i], ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def main() -> None:
    areas, seen = [], set()
    for service, pre in SETS:
        for f in get(service)["features"]:
            p, g = f["properties"], f["geometry"]
            code, name = p[pre + "CD"], re.sub(r", (City|County) of$", "", p[pre + "NM"].strip())  # "Bristol, City of"
            if code in seen:  # unitary authorities are in both the county and district sets
                continue
            seen.add(code)
            polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
            rings = sorted((poly[0] for poly in polys), key=ring_area, reverse=True)
            rings = rings[:1] + [r for r in rings[1:] if ring_area(r) >= MIN_ISLAND]
            shapes = [[[round(y, 3), round(x, 3)] for x, y in r[:-1]] for r in rings]
            lats = [pt[0] for s in shapes for pt in s]
            lons = [pt[1] for s in shapes for pt in s]
            welsh = (p.get(pre + "NMW") or "").strip()
            kind = "National park" if pre.startswith("NPARK") else KIND.get(code[:3], "Area")
            areas.append({"id": code, "name": name, **({"alt": welsh} if welsh and welsh != name else {}),
                          "kind": kind, "lat": p.get("LAT"), "lon": p.get("LONG"),
                          "bbox": [min(lats), min(lons), max(lats), max(lons)], "shapes": shapes})

    # which region (or nation) each area is in, and which county each district is in, for grouping and labels
    regions = [a for a in areas if a["kind"] == "Region"]
    counties = [a for a in areas if a["kind"] == "County"]

    def within(a: dict, among: list) -> dict | None:
        hit = next((b for b in among if any(inside(a["lat"], a["lon"], s) for s in b["shapes"])), None)
        if hit or not among:  # a centroid out at sea (the Isles of Scilly): take the nearest
            return hit
        return min(among, key=lambda b: min((pt[0] - a["lat"]) ** 2 + (pt[1] - a["lon"]) ** 2 for s in b["shapes"] for pt in s))

    for a in areas:
        if a["kind"] in ("Nation", "Region"):
            continue
        if a["id"][0] in NATION:
            a["group"] = NATION[a["id"][0]]
        elif a["kind"] == "National park":
            a["group"] = (within(a, regions) or {}).get("name", "England")
        else:
            a["group"] = within(a, regions)["name"]
        if a["kind"] == "District":
            c = within(a, counties)
            if c:
                a["parent"] = c["name"]
    for a in areas:
        del a["lat"], a["lon"]
    dest = Path(__file__).resolve().parents[1] / "uksitrep" / "data" / "areas.json"
    dest.write_text(json.dumps({"source": "ONS Open Geography Portal (OGL; contains OS data © Crown copyright)",
                                "areas": areas}, separators=(",", ":"), ensure_ascii=False))
    print(dest, dest.stat().st_size, "bytes,", len(areas), "areas,", sum(len(s) for a in areas for s in a["shapes"]), "points")


if __name__ == "__main__":
    main()
