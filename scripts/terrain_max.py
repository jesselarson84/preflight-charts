#!/usr/bin/env python3
"""Highest terrain in every sectional quadrangle, for the maximum elevation figures (MEFs).

A sectional prints one MEF in each quadrangle half a degree of latitude by half a degree of
longitude. The FAA publishes no feed of them, so build_chart.py works them out from the highest
terrain (this file's output) and the highest obstacle (the Digital Obstacle File).

Terrain hardly changes, so this runs by hand, not every cycle, and its output is committed:
    python3 tools/chart/terrain_max.py tools/chart/data/terrain_max.csv  (scripts/ in preflight-charts)
It reads the 1 arc-second (about 30 m) SRTM-based tiles in the public AWS Terrain Tiles bucket
(Mapzen "skadi" tiles; Alaska north of 60 N comes from other national elevation data), about
2,000 one-degree tiles. Needs numpy. Takes ten to twenty minutes on a fast connection.

Output: one row per quadrangle with land in it: lat,lon (its south-west corner),max_ft.
"""

import concurrent.futures
import gzip
import sys
import urllib.error
import urllib.request

import numpy as np

SKADI = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}/{name}.hgt.gz"

# The areas FAA sectionals cover, in whole degrees: (south, north, west, east).
AREAS = [
    (24, 50, -125, -66),   # Lower 48, with the border strips the sectionals print
    (51, 72, -180, -129),  # Alaska and the Aleutians west to 180
    (51, 54, 172, 180),    # the far western Aleutians
    (18, 23, -161, -154),  # Hawaii
    (17, 19, -68, -64),    # Puerto Rico and the Virgin Islands
]


def tile_name(lat, lon):
    ns = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}"
    return ns, f"{ns}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def tile_max(lat, lon):
    """The four quadrangles of one 1-degree tile: [(lat, lon, max_ft)], or [] over open sea."""
    ns, name = tile_name(lat, lon)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(SKADI.format(ns=ns, name=name), timeout=120) as r:
                raw = gzip.decompress(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return []
            if attempt == 3:
                raise
        except Exception:  # noqa: BLE001 - retried, then raised
            if attempt == 3:
                raise
    n = int(round((len(raw) // 2) ** 0.5))
    grid = np.frombuffer(raw, dtype=">i2").reshape(n, n).astype(np.int32)
    grid[grid == -32768] = -1000  # voids
    half = n // 2
    out = []
    # Row 0 is the north edge; the shared middle row and column belong to both halves.
    for dlat, rows in ((0.5, slice(0, half + 1)), (0.0, slice(half, n))):
        for dlon, cols in ((0.0, slice(0, half + 1)), (0.5, slice(half, n))):
            m = int(grid[rows, cols].max())
            if m > 0:
                out.append((lat + dlat, lon + dlon, round(m * 3.28084)))
    return out


def main():
    out_path = sys.argv[1]
    tiles = [(lat, lon) for s, n, w, e in AREAS for lat in range(s, n) for lon in range(w, e)]
    rows = []
    with concurrent.futures.ThreadPoolExecutor(12) as pool:
        for i, result in enumerate(pool.map(lambda t: tile_max(*t), tiles)):
            rows += result
            if i % 200 == 0:
                print(f"{i}/{len(tiles)} tiles, {len(rows)} quadrangles", flush=True)
    rows.sort()
    with open(out_path, "w") as fh:
        fh.write("lat,lon,max_ft\n")
        for lat, lon, ft in rows:
            fh.write(f"{lat:g},{lon:g},{ft}\n")
    print(f"wrote {out_path}: {len(rows)} quadrangles")


if __name__ == "__main__":
    main()
