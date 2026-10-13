"""Build uksitrep/data/energy_regions.json from NESO's GB DNO licence areas (EPSG:27700).

    curl -L -o dno.geojson "https://api.neso.energy/dataset/0e377f16-95e9-4c15-a1fc-49e06a39cfa0/resource/1c6a7dc0-1b6c-443a-bc67-5f7125649434/download/gb-dno-license-areas-20240503-as-geojson.geojson"
    python3 tools/build_energy_regions.py dno.geojson

The 14 areas are the regions that Carbon Intensity, PV_Live (PES id) and Octopus (letter) all use.
Small islands are dropped and the outlines simplified to about 1.5 km, which is plenty for shading.
"""

import json
import math
from pathlib import Path
import sys


def osgb_to_wgs84(e: float, n: float) -> tuple[float, float]:
    """OSGB36 National Grid -> WGS84 lat/lon (Helmert; good to ~5 m)."""
    a, b, f0 = 6377563.396, 6356256.909, 0.9996012717
    lat0, lon0, n0, e0 = math.radians(49), math.radians(-2), -100000, 400000
    e2 = 1 - b * b / (a * a)
    nn = (a - b) / (a + b)
    lat, m = lat0, 0.0
    while abs(n - n0 - m) >= 1e-5:
        lat = (n - n0 - m) / (a * f0) + lat
        ma = (1 + nn + 5 / 4 * nn ** 2 + 5 / 4 * nn ** 3) * (lat - lat0)
        mb = (3 * nn + 3 * nn ** 2 + 21 / 8 * nn ** 3) * math.sin(lat - lat0) * math.cos(lat + lat0)
        mc = (15 / 8 * nn ** 2 + 15 / 8 * nn ** 3) * math.sin(2 * (lat - lat0)) * math.cos(2 * (lat + lat0))
        md = 35 / 24 * nn ** 3 * math.sin(3 * (lat - lat0)) * math.cos(3 * (lat + lat0))
        m = b * f0 * (ma - mb + mc - md)
    s, c, t = math.sin(lat), math.cos(lat), math.tan(lat)
    nu = a * f0 / math.sqrt(1 - e2 * s * s)
    rho = a * f0 * (1 - e2) / (1 - e2 * s * s) ** 1.5
    eta2 = nu / rho - 1
    vii, viii = t / (2 * rho * nu), t / (24 * rho * nu ** 3) * (5 + 3 * t * t + eta2 - 9 * t * t * eta2)
    ix = t / (720 * rho * nu ** 5) * (61 + 90 * t * t + 45 * t ** 4)
    x, xi = 1 / c / nu, 1 / c / (6 * nu ** 3) * (nu / rho + 2 * t * t)
    xii = 1 / c / (120 * nu ** 5) * (5 + 28 * t * t + 24 * t ** 4)
    xiia = 1 / c / (5040 * nu ** 7) * (61 + 662 * t * t + 1320 * t ** 4 + 720 * t ** 6)
    de = e - e0
    lat = lat - vii * de ** 2 + viii * de ** 4 - ix * de ** 6
    lon = lon0 + x * de - xi * de ** 3 + xii * de ** 5 - xiia * de ** 7
    # OSGB36 -> WGS84 via cartesian Helmert
    s, c = math.sin(lat), math.cos(lat)
    nu = a / math.sqrt(1 - e2 * s * s)
    x1, y1, z1 = nu * c * math.cos(lon), nu * c * math.sin(lon), (1 - e2) * nu * s
    tx, ty, tz, sc = 446.448, -125.157, 542.060, -20.4894e-6
    rx, ry, rz = (math.radians(v / 3600) for v in (0.1502, 0.2470, 0.8421))
    x2 = tx + (1 + sc) * x1 - rz * y1 + ry * z1
    y2 = ty + rz * x1 + (1 + sc) * y1 - rx * z1
    z2 = tz - ry * x1 + rx * y1 + (1 + sc) * z1
    a2, b2 = 6378137.0, 6356752.3142
    e22 = 1 - b2 * b2 / (a2 * a2)
    p = math.hypot(x2, y2)
    lat = math.atan2(z2, p * (1 - e22))
    for _ in range(10):
        nu = a2 / math.sqrt(1 - e22 * math.sin(lat) ** 2)
        lat = math.atan2(z2 + e22 * nu * math.sin(lat), p)
    return math.degrees(lat), math.degrees(math.atan2(y2, x2))


def simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker on (x, y) metres."""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = pts[i], pts[j]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1e-9
        best, k = -1.0, -1
        for q in range(i + 1, j):
            d = abs(dy * (pts[q][0] - x1) - dx * (pts[q][1] - y1)) / L
            if d > best:
                best, k = d, q
        if best > tol:
            keep[k] = True
            stack += [(i, k), (k, j)]
    return [p for p, kp in zip(pts, keep) if kp]


def ring_area(r: list) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(r, r[1:] + r[:1]))) / 2


def main(src: str) -> None:
    d = json.loads(Path(src).read_text())
    out = []
    for f in d["features"]:
        p, g = f["properties"], f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        shapes = []
        for poly in polys:
            outer = poly[0]
            if ring_area(outer) < 40e6:  # islands under 40 km² add bytes, not information
                continue
            # a closed ring has no chord to measure from: split it at the point farthest from the start
            o = outer[:-1] if outer[0] == outer[-1] else outer
            k = max(range(len(o)), key=lambda i: math.hypot(o[i][0] - o[0][0], o[i][1] - o[0][1]))
            ring = simplify(o[:k + 1], 1500)[:-1] + simplify(o[k:] + o[:1], 1500)[:-1]
            if len(ring) >= 4:
                shapes.append([[round(v, 3) for v in osgb_to_wgs84(x, y)] for x, y in ring])
        out.append({"pes": p["ID"], "letter": p["Name"].strip("_"), "name": p["Area"], "dno": p["DNO_Full"], "shapes": shapes})
    out.sort(key=lambda r: r["pes"])
    dest = Path(__file__).resolve().parents[1] / "uksitrep" / "data" / "energy_regions.json"
    dest.write_text(json.dumps({"source": "NESO GB DNO licence areas 2024-05-03 (NESO Open Data Licence)", "regions": out},
                               separators=(",", ":")))
    print(dest, dest.stat().st_size, "bytes", sum(len(s) for r in out for s in r["shapes"]), "points")


if __name__ == "__main__":
    main(sys.argv[1])
