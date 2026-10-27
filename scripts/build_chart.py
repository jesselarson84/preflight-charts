#!/usr/bin/env python3
"""Builds Preflight's aeronautical chart data from the FAA's own published sources.

Every 28-day cycle the FAA republishes what a VFR sectional chart is drawn from. This script
turns it into GeoJSON layers that tippecanoe packs into one PMTiles file the app streams:

  airspace   Class B, C, D and E with floors and ceilings (FAA AIS open data)
  sua        Prohibited, restricted, warning, alert areas and MOAs (FAA AIS open data)
  modec      30 NM Mode C veils around Class B airports (FAA AIS open data)
  laanc      UAS facility map grid: the most LAANC can approve per square (FAA AIS open data)
  airports   Airports and heliports with elevation, tower, frequencies, runway (NASR)
  runways    Runway centrelines (NASR)
  navaids    VORs and NDBs with frequencies (NASR)
  obstacles  Obstacles 200 ft and taller, as on a sectional (Digital Obstacle File)
  airways    Victor airways and T routes, segment by segment, with mileage (NASR)
  waypoints  VFR waypoints (NASR)
  isogonic   Isogonic lines every degree, from the World Magnetic Model (NOAA)
  mef        Maximum elevation figures for each half-degree quadrangle (terrain and DOF)

The FAA publishes no feed of isogonic lines or MEFs, so they're worked out here the way the FAA
does: declination from the World Magnetic Model coefficients in data/WMM2025.COF, and MEFs from the
highest terrain in each quadrangle (data/terrain_max.csv, made by terrain_max.py) and the highest
obstacle in the current Digital Obstacle File.

Standard library only, so it runs on a bare CI runner. Prints a summary of every source so a
change in the FAA's files shows up in the build log rather than as a silently empty layer.
"""

import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile

ARCGIS = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"
CLASS_LAYER = f"{ARCGIS}/Class_Airspace/FeatureServer/0"
SUA_LAYER = f"{ARCGIS}/Special_Use_Airspace/FeatureServer/0"
LAANC_LAYER = f"{ARCGIS}/FAA_UAS_FacilityMap_Data/FeatureServer/0"
NASR_CSV = "https://nfdc.faa.gov/webContent/28DaySub/extra/{date}_CSV.zip"
DOF_CSV = "https://aeronav.faa.gov/Obst_Data/DAILY_DOF_CSV.ZIP"

# AIRAC cycles run every 28 days from this effective date.
AIRAC_EPOCH = dt.date(2025, 1, 23)

USER_AGENT = "Preflight-chart-builder (+https://github.com/jesselarson84)"


def log(*a):
    print(*a, flush=True)


def get(url, binary=False, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - retried, then raised
            if attempt == tries - 1:
                raise
            log(f"  retry {attempt + 1} for {url[:120]}: {e}")
            time.sleep(2 ** (attempt + 1))


# ArcGIS feature services ---------------------------------------------------------------------


def arcgis_features(layer, where="1=1", page=500):
    """Every feature in a layer as GeoJSON, following the server's paging.

    Big airspace polygons can time the server out, so a page that fails is retried at half
    the size, down to a handful of features. Geometry is lightly simplified (about 10 m),
    far below what shows on a phone.
    """
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "where": where, "outFields": "*", "outSR": "4326", "f": "geojson",
            "geometryPrecision": "5", "maxAllowableOffset": "0.0001", "orderByFields": "OBJECTID",
            "resultOffset": offset, "resultRecordCount": page,
        })
        try:
            body = json.loads(get(f"{layer}/query?{q}", tries=2))
        except Exception as e:  # noqa: BLE001 - shrink the page and try again
            if page <= 10:
                raise
            page //= 2
            log(f"  {e}; retrying with pages of {page}")
            continue
        if "error" in body:
            if body["error"].get("code") == 429:
                # The FAA's service allows a fixed amount of work per minute.
                log("  rate limited; waiting a minute")
                time.sleep(61)
                continue
            raise RuntimeError(f"{layer}: {body['error']}")
        feats = body.get("features", [])
        out += feats
        more = body.get("exceededTransferLimit") or body.get("properties", {}).get("exceededTransferLimit")
        if not feats or (not more and len(feats) < page):
            break
        offset += len(feats)
        if len(out) % 5000 < len(feats):
            log(f"  {len(out)} features so far")
    return out


def describe(name, feats, *keys):
    log(f"{name}: {len(feats)} features")
    for k in keys:
        counts = {}
        for f in feats:
            v = f.get("properties", {}).get(k)
            counts[v] = counts.get(v, 0) + 1
        log(f"  {k}: " + ", ".join(f"{v}={n}" for v, n in sorted(counts.items(), key=lambda x: -x[1])[:20]))
    if feats:
        props = feats[0].get("properties", {})
        log("  fields:", ", ".join(sorted(props)))
        log("  sample:", json.dumps({k: props[k] for k in list(props)[:14]})[:600])


def up(v):
    return (str(v).strip().upper() if v is not None else "")


def num(v):
    try:
        f = float(str(v).strip())
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def limit_text(p, prefix):
    """Floor or ceiling as a sectional prints it: SFC, or hundreds of feet.

    The FAA's code SFC is the reference (feet above the surface), not the value: an E5 floor is
    700 with code SFC, meaning 700 ft AGL. Only a value of 0 is the surface itself.
    """
    code, val, uom = up(p.get(f"{prefix}_CODE")), num(p.get(f"{prefix}_VAL")), up(p.get(f"{prefix}_UOM"))
    if val == 0 or (val is None and code == "SFC" and prefix == "LOWER"):
        return "SFC", 0, code or "SFC"
    if val is None or val < 0:
        return None, None, code
    ft = val * 100 if uom == "FL" else val
    if prefix == "UPPER" and ft >= 18000:
        return "", int(ft), code
    return str(int(round(ft / 100))), int(ft), code


def airspace_layer(feats):
    out = []
    for f in feats:
        p = f.get("properties", {})
        if up(p.get("TYPE_CODE")) == "MODE-C":
            continue  # drawn from modec_layer
        cls = up(p.get("CLASS")).replace("CLASS", "").strip()
        local = up(p.get("LOCAL_TYPE"))
        lo_txt, lo_ft, lo_code = limit_text(p, "LOWER")
        hi_txt, hi_ft, _ = limit_text(p, "UPPER")
        if cls in ("B", "C", "D"):
            kind = cls
        elif cls == "E" or local.startswith("CLASS_E"):
            # The local type is the reliable signal; the floor fills in only when it's missing.
            # Only E2, E3 and E4 are surface areas; E5, E6 and E7 never reach the ground.
            if local == "CLASS_E5":
                kind = "E_1200" if lo_ft is not None and lo_ft >= 1200 else "E_700"
            elif local in ("CLASS_E2", "CLASS_E3", "CLASS_E4"):
                kind = "E_SFC"
            elif local.startswith("CLASS_E") and local != "CLASS_E":
                kind = "E_OTHER"  # E6 en-route and E7 offshore: not drawn on a sectional
            elif lo_ft == 0:
                kind = "E_SFC"
            elif lo_ft in (700, 1200):
                kind = "E_1200" if lo_ft >= 1200 else "E_700"
            else:
                kind = "E_OTHER"
        else:
            continue
        # Shelf label as printed: ceiling over floor for B and C, the ceiling for D (the app frames it).
        label = ""
        if kind in ("B", "C") and hi_txt is not None and lo_txt is not None:
            label = f"{hi_txt}\n{lo_txt}"
        elif kind == "D" and hi_txt:
            label = hi_txt
        out.append(feature(f["geometry"], {
            "kind": kind,
            "name": p.get("NAME") or "",
            "floor_ft": lo_ft if lo_ft is not None else -1,
            "floor_ref": lo_code,
            "ceiling_ft": hi_ft if hi_ft is not None else -1,
            "label": label,
        }, minzoom=4 if kind in ("B", "C") else 6 if kind in ("D", "E_SFC") else 7))
    return out


def sua_layer(feats):
    out = []
    for f in feats:
        p = f.get("properties", {})
        t = up(p.get("TYPE_CODE"))
        if t not in ("P", "R", "W", "A", "MOA", "NSA"):
            continue
        lo_txt, lo_ft, _ = limit_text(p, "LOWER")
        hi_txt, hi_ft, _ = limit_text(p, "UPPER")
        out.append(feature(f["geometry"], {
            "kind": t,
            "name": p.get("NAME") or "",
            "floor_ft": lo_ft if lo_ft is not None else -1,
            "ceiling_ft": hi_ft if hi_ft is not None else -1,
            "times": p.get("TIMESOFUSE") or p.get("TIMES_OF_USE") or "",
        }, minzoom=5))
    return out


def laanc_layer(feats):
    out = []
    for f in feats:
        p = f.get("properties", {})
        c = num(p.get("CEILING"))
        if c is None:
            continue
        out.append(feature(f["geometry"], {"ceiling": int(c), "airport": p.get("APT1_FAAID") or ""}, minzoom=9))
    return out


# NASR ----------------------------------------------------------------------------------------


def airac_dates(today):
    n = (today - AIRAC_EPOCH).days // 28
    current = AIRAC_EPOCH + dt.timedelta(days=28 * n)
    return [current, current - dt.timedelta(days=28)]


def nasr_tables(today):
    """The NASR CSV tables for the current cycle, falling back to the last one if it isn't out yet."""
    for d in airac_dates(today):
        url = NASR_CSV.format(date=d.strftime("%d_%b_%Y"))
        try:
            data = get(url, binary=True)
        except Exception as e:  # noqa: BLE001
            log(f"NASR {d}: not available ({e})")
            continue
        log(f"NASR cycle {d}: {len(data) // 1024} KB from {url}")
        tables = {}
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                base = os.path.basename(name).upper()
                if base.endswith(".CSV"):
                    tables[base[:-4]] = name
            want = ["APT_BASE", "APT_RWY", "APT_RWY_END", "FRQ", "NAV_BASE", "AWY_SEG_ALT", "FIX_BASE"]
            log("  tables:", ", ".join(sorted(tables)))
            rows = {}
            for t in want:
                if t not in tables:
                    log(f"  missing table {t}")
                    rows[t] = []
                    continue
                text = z.read(tables[t]).decode("latin-1")
                rows[t] = list(csv.DictReader(io.StringIO(text)))
                log(f"  {t}: {len(rows[t])} rows; columns: {', '.join(rows[t][0].keys()) if rows[t] else '-'}")
        return d, rows
    raise RuntimeError("No NASR CSV subscription found for the current or previous cycle")


def col(row, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return v.strip()
    return ""


HARD_SURFACES = ("ASPH", "CONC", "BITU", "PEM", "PFC")


def hard_surface(code):
    """Asphalt, concrete and the like; NASR combines codes such as ASPH-TURF, so the first part decides."""
    return up(code).split("-")[0].split("/")[0] in HARD_SURFACES


def runway_info(rows):
    """Per airport: longest runway, longest hard runway and its true heading, and whether any is lit."""
    heading = {}
    for r in rows["APT_RWY_END"]:
        key = (col(r, "SITE_NO"), col(r, "RWY_ID"))
        if key not in heading:
            t = num(col(r, "TRUE_ALIGNMENT"))
            if t is not None:
                heading[key] = t % 180
    info = {}
    for r in rows["APT_RWY"]:
        site, rid = col(r, "SITE_NO"), col(r, "RWY_ID")
        length = num(col(r, "RWY_LEN")) or 0
        i = info.setdefault(site, {"longest": 0, "hard": 0, "hdg": None, "lit": False, "hard_count": 0})
        i["longest"] = max(i["longest"], length)
        if hard_surface(col(r, "SURFACE_TYPE_CODE")) and not rid.upper().startswith("H"):
            i["hard_count"] += 1
            if length > i["hard"]:
                i["hard"] = length
                i["hdg"] = heading.get((site, rid))
        if col(r, "RWY_LGT_CODE") not in ("", "NONE", "NSTD"):
            i["lit"] = True
    return info


# FAA chart symbol for each airport, as in the sectional legend.
def airport_symbol(site_type, private, military, info):
    if site_type == "H":
        return "heli"
    if site_type == "S":
        return "sea"
    if site_type == "U":
        return "ultra"
    if private:
        return "pvt"
    if info["hard"] > 8069:
        return "big"
    if info["hard"] >= 1500:
        return "hard"
    return "mil" if military else "soft"


def airports_layer(rows):
    rinfo = runway_info(rows)

    freqs = {}
    for r in rows["FRQ"]:
        fac = col(r, "SERVICED_FACILITY", "FACILITY")
        use = up(col(r, "FREQ_USE"))
        freq = col(r, "FREQ")
        if not fac or not freq:
            continue
        f = freqs.setdefault(fac, {})
        if "CTAF" in use and "ctaf" not in f:
            f["ctaf"] = freq
        if ("LCL" in use or "TWR" in up(col(r, "FACILITY_TYPE"))) and "twr" not in f:
            f["twr"] = freq
        if "UNICOM" in use and "unicom" not in f:
            f["unicom"] = freq

    out = []
    for r in rows["APT_BASE"]:
        if up(col(r, "ARPT_STATUS")) not in ("", "O"):
            continue
        lat, lon = num(col(r, "LAT_DECIMAL")), num(col(r, "LONG_DECIMAL"))
        if lat is None or lon is None:
            continue
        ident = col(r, "ARPT_ID")
        site_type = up(col(r, "SITE_TYPE_CODE"))  # A airport, H heliport, S seaplane, U ultralight, G glider, B balloon
        towered = up(col(r, "TWR_TYPE_CODE")).startswith("ATCT")
        private = up(col(r, "FACILITY_USE_CODE")) == "PR"
        military = up(col(r, "OWNERSHIP_TYPE_CODE")) in ("MA", "MN", "MR", "CG")
        info = rinfo.get(col(r, "SITE_NO"), {"longest": 0, "hard": 0, "hdg": None, "lit": False, "hard_count": 0})
        f = freqs.get(ident, {})
        rwy = int(info["longest"])
        symbol = airport_symbol(site_type, private, military, info)
        major = towered and rwy >= 8000
        out.append(feature({"type": "Point", "coordinates": [lon, lat]}, {
            "id": ident,
            "icao": col(r, "ICAO_ID"),
            "name": col(r, "ARPT_NAME").upper(),
            "type": site_type or "A",
            "symbol": symbol,
            "hdg": round(info["hdg"]) if info["hdg"] is not None else 0,
            "towered": towered,
            "private": private,
            "military": military,
            "elev": int(num(col(r, "ELEV")) or 0),
            "rwy": rwy,
            # Runway length in hundreds of feet, as printed; heliports and tiny strips get none.
            "rwy_label": str(round(rwy / 100)) if site_type in ("", "A") and rwy >= 1000 else "",
            "lit": info["lit"],
            "beacon": bool(col(r, "BCN_LENS_COLOR")) and up(col(r, "BCN_LENS_COLOR")) not in ("N", "NONE"),
            "fuel": bool(col(r, "FUEL_TYPES")),
            "ctaf": col(r, "CTAF_FREQ") or f.get("ctaf", ""),
            "twr": f.get("twr", ""),
            "unicom": f.get("unicom", ""),
            "rank": 0 if major else 1 if towered else 2 if not private else 3,
        }, minzoom=4 if major else 6 if towered else 8 if not private else 11))
    return out


def runways_layer(rows, airports):
    """Runway outlines for the airports the sectional draws as a runway pattern."""
    by_site = {}
    for r in rows["APT_BASE"]:
        by_site[col(r, "SITE_NO")] = col(r, "ARPT_ID")
    apt = {a["properties"]["id"]: a["properties"] for a in airports}
    surface = {(col(r, "SITE_NO"), col(r, "RWY_ID")): hard_surface(col(r, "SURFACE_TYPE_CODE")) for r in rows["APT_RWY"]}
    ends = {}
    for r in rows["APT_RWY_END"]:
        lat, lon = num(col(r, "LAT_DECIMAL")), num(col(r, "LONG_DECIMAL"))
        if lat is None or lon is None:
            continue
        ends.setdefault((col(r, "SITE_NO"), col(r, "RWY_ID")), []).append([lon, lat])
    out = []
    for (site, rwy), pts in ends.items():
        a = apt.get(by_site.get(site, ""))
        if len(pts) < 2 or a is None or not surface.get((site, rwy)):
            continue
        out.append(feature({"type": "LineString", "coordinates": pts[:2]}, {
            "rwy": rwy, "towered": a["towered"], "big": a["symbol"] == "big",
        }, minzoom=8 if a["symbol"] == "big" else 11))
    return out


def navaids_layer(rows):
    out = []
    for r in rows["NAV_BASE"]:
        lat, lon = num(col(r, "LAT_DECIMAL")), num(col(r, "LONG_DECIMAL"))
        t = up(col(r, "NAV_TYPE"))
        if lat is None or lon is None or not t or up(col(r, "NAV_STATUS")) in ("DECOMMISSIONED", "SHUTDOWN"):
            continue
        out.append(feature({"type": "Point", "coordinates": [lon, lat]}, {
            "id": col(r, "NAV_ID"),
            "name": col(r, "NAME").title(),
            "type": t,
            "freq": col(r, "FREQ"),
            # Ident in Morse code is printed in the box; only the letters are carried here.
        }, minzoom=7 if "VOR" in t or "DME" in t or "TACAN" in t else 9))
    return out


# Obstacles -----------------------------------------------------------------------------------


def obstacles_layer():
    """Obstacles 200 ft and up for the chart, and the highest obstacle top in every quadrangle for the MEFs."""
    data = get(DOF_CSV, binary=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.upper().endswith(".CSV"))
        text = z.read(name).decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [h.strip().upper() for h in reader.fieldnames]
    log(f"DOF {name}: columns: {', '.join(reader.fieldnames)}")
    out, highest = [], {}
    for r in reader:
        agl, amsl = num(col(r, "AGL")), num(col(r, "AMSL"))
        lat, lon = num(col(r, "LATDEC")), num(col(r, "LONDEC"))
        if lat is not None and lon is not None and amsl is not None:
            q = quadrangle(lat, lon)
            highest[q] = max(highest.get(q, 0), int(amsl))
        if agl is None or lat is None or lon is None or agl < 200:
            continue
        lt = up(col(r, "LT", "LIGHTING"))
        out.append(feature({"type": "Point", "coordinates": [lon, lat]}, {
            "agl": int(agl),
            "amsl": int(amsl or 0),
            "lit": lt not in ("", "N", "U"),
            "type": col(r, "TYPE", "OBSTACLE TYPE").title(),
            "count": int(num(col(r, "QUANTITY")) or 1),
            "tall": agl >= 1000,
            # High-intensity lights get the flash marks on the chart.
            "hi": lt in ("H", "S"),
            "wind": "WIND" in up(col(r, "TYPE", "OBSTACLE TYPE")),
            # Not verified by the FAA: the chart prints UC under its height.
            "uc": up(col(r, "VERIFIED STATUS", "VERIFIED")) == "U",
        }, minzoom=7 if agl >= 1000 else 9 if agl >= 500 else 11))
    log(f"obstacles >= 200 ft AGL: {len(out)}, unverified (UC): {sum(1 for f in out if f['properties']['uc'])}")
    return out, highest


# Airways and VFR waypoints ------------------------------------------------------------------


def point_index(rows):
    """Every navaid and fix by identifier, as [(lon, lat)]: one identifier can name several."""
    points = {}
    for r in rows["NAV_BASE"]:
        lat, lon = num(col(r, "LAT_DECIMAL")), num(col(r, "LONG_DECIMAL"))
        if lat is not None and lon is not None:
            points.setdefault(up(col(r, "NAV_ID")), []).append((lon, lat))
    for r in rows["FIX_BASE"]:
        lat, lon = num(col(r, "LAT_DECIMAL")), num(col(r, "LONG_DECIMAL"))
        if lat is not None and lon is not None:
            points.setdefault(up(col(r, "FIX_ID")), []).append((lon, lat))
    return points


def nearest(candidates, near):
    if not candidates:
        return None
    if near is None:
        return candidates[0]
    return min(candidates, key=lambda p: (p[0] - near[0]) ** 2 + (p[1] - near[1]) ** 2)


def airways_layer(rows):
    """Victor airways and T routes, the low routes a sectional draws, one feature per segment.

    Airways that share a segment are drawn once and labelled together, as the chart does: V3-140.
    A segment longer than 300 NM or one the FAA flags as a gap is left out rather than drawn
    across country it doesn't follow.
    """
    points = point_index(rows)
    routes = {}
    for r in rows["AWY_SEG_ALT"]:
        ident = up(col(r, "AWY_ID")).replace(" ", "")
        if not ident or ident[0] not in "VT" or not ident[1:].isdigit():
            continue
        key = (ident, up(col(r, "AWY_LOCATION")))
        routes.setdefault(key, []).append(r)
    log(f"airways: {len(routes)} Victor and T routes; point types: " + ", ".join(
        f"{k}={n}" for k, n in sorted(count_by(rows["AWY_SEG_ALT"], "FROM_PT_TYPE").items(), key=lambda x: -x[1])[:12]))
    segments, missing = {}, set()
    for (ident, _), segs in routes.items():
        segs.sort(key=lambda r: num(col(r, "POINT_SEQ")) or 0)
        prev = None
        for i, r in enumerate(segs):
            a = up(col(r, "FROM_POINT"))
            b = up(col(r, "TO_POINT")) or (up(col(segs[i + 1], "FROM_POINT")) if i + 1 < len(segs) else "")
            if prev is None and points.get(b):
                # The first point of a route: of the places with its name, the one nearest the next point.
                prev = min(points[b], key=lambda q: min(((q[0] - c[0]) ** 2 + (q[1] - c[1]) ** 2 for c in points.get(a, [q])), default=0))
            pa = nearest(points.get(a, []), prev)
            pb = nearest(points.get(b, []), pa)
            prev = pb
            if not pa or not pb:
                missing.update(x for x, p in ((a, pa), (b, pb)) if x and not p)
                continue
            if up(col(r, "AWY_SEG_GAP_FLAG")) == "Y" or pa == pb or nm(pa, pb) > 300:
                continue
            k = (pa, pb) if pa < pb else (pb, pa)
            seg = segments.setdefault(k, {"ids": [], "dist": None, "course": None, "from": pa})
            if ident not in seg["ids"]:
                seg["ids"].append(ident)
            if seg["dist"] is None:
                seg["dist"] = num(col(r, "MAG_COURSE_DIST"))
                seg["course"] = num(col(r, "MAG_COURSE")) if seg["from"] == pa else num(col(r, "OPP_MAG_COURSE"))
    if missing:
        log(f"  {len(missing)} airway points not found, e.g. {sorted(missing)[:10]}")
    out = []
    for (pa, pb), seg in segments.items():
        a, b = seg["from"], pb if seg["from"] == pa else pa
        out.append(feature({"type": "LineString", "coordinates": [list(a), list(b)]}, {
            "id": airway_label(seg["ids"]),
            "victor": any(i.startswith("V") for i in seg["ids"]),
            "dist": int(round(seg["dist"])) if seg["dist"] else 0,
            "course": int(round(seg["course"])) % 360 if seg["course"] is not None else -1,
        }, minzoom=6))
    log(f"airway segments drawn: {len(out)}")
    return out


def airway_label(ids):
    """V3, V140 and T201 on one segment print as V3-140 T201."""
    out = []
    for prefix in "VT":
        nums = sorted((i[1:] for i in ids if i.startswith(prefix)), key=int)
        if nums:
            out.append(prefix + "-".join(nums))
    return " ".join(out)


def nm(a, b):
    """Great-circle distance in nautical miles between (lon, lat) points."""
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1, math.sqrt(d)))


def count_by(rows, column):
    counts = {}
    for r in rows:
        v = up(col(r, column)) or "-"
        counts[v] = counts.get(v, 0) + 1
    return counts


def waypoints_layer(rows):
    """VFR waypoints: the FAA names them VP and three letters and marks their use VFR."""
    log("fix use codes: " + ", ".join(f"{k}={n}" for k, n in sorted(count_by(rows["FIX_BASE"], "FIX_USE_CODE").items(), key=lambda x: -x[1])[:15]))
    out = []
    for r in rows["FIX_BASE"]:
        ident = up(col(r, "FIX_ID"))
        lat, lon = num(col(r, "LAT_DECIMAL")), num(col(r, "LONG_DECIMAL"))
        if lat is None or lon is None:
            continue
        if not (up(col(r, "FIX_USE_CODE")) == "VFR" or (ident.startswith("VP") and len(ident) == 5)):
            continue
        out.append(feature({"type": "Point", "coordinates": [lon, lat]}, {"id": ident}, minzoom=8))
    log(f"VFR waypoints: {len(out)}")
    return out


# Isogonic lines ------------------------------------------------------------------------------

WMM_COF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "WMM2025.COF")


class MagneticModel:
    """The World Magnetic Model (NOAA/BGS), enough of it for declination at sea level."""

    def __init__(self, path=WMM_COF):
        self.g, self.h, self.gd, self.hd = {}, {}, {}, {}
        with open(path) as fh:
            self.epoch = float(fh.readline().split()[0])
            for line in fh:
                p = line.split()
                if len(p) < 6 or line.startswith("9999"):
                    continue
                n, m = int(p[0]), int(p[1])
                self.g[n, m], self.h[n, m], self.gd[n, m], self.hd[n, m] = map(float, p[2:6])
        self.max_n = max(n for n, _ in self.g)

    def declination(self, lat, lon, year):
        """Degrees east of true north (negative is west), at sea level."""
        a, f, re = 6378.137, 1 / 298.257223563, 6371.2
        e2 = f * (2 - f)
        phi, lam = math.radians(lat), math.radians(lon)
        rc = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
        p, z = rc * math.cos(phi), rc * (1 - e2) * math.sin(phi)
        r = math.hypot(p, z)
        phc = math.asin(z / r)
        mu, c = math.sin(phc), math.cos(phc)
        dt = year - self.epoch
        # Schmidt semi-normalised associated Legendre functions of sin(geocentric latitude).
        P = {(0, 0): 1.0}
        for n in range(1, self.max_n + 1):
            for m in range(n + 1):
                if m == n:
                    P[n, m] = c if n == 1 else math.sqrt((2 * n - 1) / (2 * n)) * c * P[n - 1, n - 1]
                else:
                    P[n, m] = ((2 * n - 1) * mu * P[n - 1, m] - math.sqrt((n - 1) ** 2 - m * m) * P.get((n - 2, m), 0.0)) / math.sqrt(n * n - m * m)
        x = y = zz = 0.0
        for n in range(1, self.max_n + 1):
            k = (re / r) ** (n + 2)
            for m in range(n + 1):
                g = self.g[n, m] + dt * self.gd[n, m]
                h = self.h[n, m] + dt * self.hd[n, m]
                cm, sm = math.cos(m * lam), math.sin(m * lam)
                dp = (-n * mu * P[n, m] + math.sqrt(n * n - m * m) * P.get((n - 1, m), 0.0)) / c
                x -= k * (g * cm + h * sm) * dp
                y += k * m * (g * sm - h * cm) * P[n, m] / c
                zz -= k * (n + 1) * (g * cm + h * sm) * P[n, m]
        # Back from geocentric to geodetic north.
        psi = phc - phi
        north = x * math.cos(psi) - zz * math.sin(psi)
        return math.degrees(math.atan2(y, north))


def contours(grid, lats, lons, level):
    """Marching squares: the polylines where grid crosses level, joined end to end."""
    segs = []
    for i in range(len(lats) - 1):
        for j in range(len(lons) - 1):
            corners = [(lats[i], lons[j], grid[i][j]), (lats[i], lons[j + 1], grid[i][j + 1]),
                       (lats[i + 1], lons[j + 1], grid[i + 1][j + 1]), (lats[i + 1], lons[j], grid[i + 1][j])]
            cross = []
            for k in range(4):
                (y1, x1, v1), (y2, x2, v2) = corners[k], corners[(k + 1) % 4]
                if (v1 < level) != (v2 < level):
                    t = (level - v1) / (v2 - v1)
                    cross.append((round(x1 + t * (x2 - x1), 5), round(y1 + t * (y2 - y1), 5)))
            if len(cross) == 2:
                segs.append(tuple(cross))
            elif len(cross) == 4:
                segs += [(cross[0], cross[1]), (cross[2], cross[3])]
    # Join the pieces: every crossing point is shared by the two cells either side of it.
    ends = {}
    for s in segs:
        for p in s:
            ends.setdefault(p, []).append(s)
    used, lines = set(), []
    for s in segs:
        if s in used:
            continue
        used.add(s)
        line = list(s)
        for forward in (True, False):
            while True:
                tip = line[-1] if forward else line[0]
                nxt = next((t for t in ends.get(tip, []) if t not in used), None)
                if nxt is None:
                    break
                used.add(nxt)
                other = nxt[1] if nxt[0] == tip else nxt[0]
                if forward:
                    line.append(other)
                else:
                    line.insert(0, other)
        if len(line) > 2:
            lines.append(line)
    return lines


def isogonic_layer(today):
    """Lines of equal magnetic variation every degree, for today's date, over the area sectionals cover."""
    model = MagneticModel()
    year = today.year + (today.timetuple().tm_yday - 0.5) / 365.25
    step = 0.25
    lats = [10 + i * step for i in range(int((75 - 10) / step) + 1)]
    lons = [-180 + j * step for j in range(int((-60 + 180) / step) + 1)]
    grid = [[model.declination(la, lo, year) for lo in lons] for la in lats]
    lo_v = math.floor(min(min(r) for r in grid))
    hi_v = math.ceil(max(max(r) for r in grid))
    log(f"isogonic lines: model epoch {model.epoch}, date {year:.2f}, declination {lo_v} to {hi_v} degrees; "
        f"Denver {model.declination(39.74, -104.99, year):.1f}, Charlotte {model.declination(35.23, -80.84, year):.1f}")
    out = []
    for level in range(lo_v, hi_v + 1):
        label = f"{abs(level)}°{'E' if level > 0 else 'W' if level < 0 else ''}"
        for line in contours(grid, lats, lons, level):
            out.append(feature({"type": "LineString", "coordinates": [list(p) for p in line]},
                               {"deg": level, "label": label}, minzoom=4))
    return out


# Maximum elevation figures -------------------------------------------------------------------

TERRAIN_MAX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "terrain_max.csv")


def quadrangle(lat, lon):
    """South-west corner of the half-degree quadrangle a point is in."""
    return math.floor(lat * 2) / 2, math.floor(lon * 2) / 2


def round_up_100(ft):
    return int(math.ceil(ft / 100.0) * 100)


def mef_layer(highest_obstacle):
    """A maximum elevation figure in every quadrangle, worked out as the FAA does.

    Terrain: the highest point, plus 200 ft for obstacles too small to chart and 100 ft for
    vertical error, rounded up to the next 100 ft. Obstacle: its top above sea level plus 100 ft,
    rounded up. The MEF is the higher of the two. Printed in thousands and hundreds of feet.
    """
    terrain = {}
    with open(TERRAIN_MAX) as fh:
        for r in csv.DictReader(fh):
            terrain[float(r["lat"]), float(r["lon"])] = int(r["max_ft"])
    out, by_obstacle = [], 0
    for q in sorted(set(terrain) | set(highest_obstacle)):
        t = round_up_100(terrain[q] + 300) if q in terrain else 0
        o = round_up_100(highest_obstacle[q] + 100) if q in highest_obstacle else 0
        mef = max(t, o)
        if mef <= 0:
            continue
        by_obstacle += o > t
        out.append(feature({"type": "Point", "coordinates": [q[1] + 0.25, q[0] + 0.25]}, {
            "mef": mef,
            "thousands": str(mef // 1000),
            "hundreds": str(mef % 1000 // 100),
            "obstacle": o > t,
        }, minzoom=6))
    log(f"MEFs: {len(out)} quadrangles, {by_obstacle} set by an obstacle")
    return out


# Mode C veils --------------------------------------------------------------------------------


def modec_layer(feats):
    """The FAA publishes the 30 NM Mode C veils with the class airspace."""
    return [feature(f["geometry"], {"name": f.get("properties", {}).get("NAME") or ""}, minzoom=5)
            for f in feats if up(f.get("properties", {}).get("TYPE_CODE")) == "MODE-C" and f.get("geometry")]


# Output --------------------------------------------------------------------------------------


def feature(geometry, props, minzoom=None):
    f = {"type": "Feature", "geometry": geometry, "properties": props}
    if minzoom is not None:
        f["tippecanoe"] = {"minzoom": minzoom}
    return f


def write(out_dir, name, feats):
    path = os.path.join(out_dir, f"{name}.geojson")
    with open(path, "w") as fh:
        fh.write('{"type":"FeatureCollection","features":[\n')
        for i, f in enumerate(feats):
            if f.get("geometry"):
                fh.write(("," if i else "") + json.dumps(f, separators=(",", ":")) + "\n")
        fh.write("]}\n")
    log(f"wrote {path}: {len(feats)} features, {os.path.getsize(path) // 1024} KB")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "build/chart"
    os.makedirs(out_dir, exist_ok=True)
    today = dt.date.today()

    cls = arcgis_features(CLASS_LAYER)
    describe("Class airspace", cls, "CLASS", "LOCAL_TYPE", "TYPE_CODE")
    floors = {}
    for f in cls:
        p = f.get("properties", {})
        if up(p.get("CLASS")) == "E" or up(p.get("LOCAL_TYPE")).startswith("CLASS_E"):
            k = (up(p.get("LOCAL_TYPE")) or "-", up(p.get("LOWER_CODE")) or "-", p.get("LOWER_VAL"), up(p.get("LOWER_UOM")) or "-")
            floors[k] = floors.get(k, 0) + 1
    log("  Class E floors (type, code, value, unit): " + ", ".join(f"{k}={n}" for k, n in sorted(floors.items(), key=lambda x: -x[1])[:30]))
    sua = arcgis_features(SUA_LAYER)
    describe("Special use airspace", sua, "TYPE_CODE")
    laanc = arcgis_features(LAANC_LAYER)
    describe("UAS facility map", laanc, "CEILING")

    cycle, rows = nasr_tables(today)
    airports = airports_layer(rows)

    airspace = airspace_layer(cls)
    describe("Airspace kinds drawn", airspace, "kind")

    obstacles, highest_obstacle = obstacles_layer()
    layers = {
        "airspace": airspace,
        "sua": sua_layer(sua),
        "laanc": laanc_layer(laanc),
        "airports": airports,
        "runways": runways_layer(rows, airports),
        "navaids": navaids_layer(rows),
        "obstacles": obstacles,
        "modec": modec_layer(cls),
        "airways": airways_layer(rows),
        "waypoints": waypoints_layer(rows),
        "isogonic": isogonic_layer(today),
        "mef": mef_layer(highest_obstacle),
    }
    for name, feats in layers.items():
        write(out_dir, name, feats)
        if not feats:
            raise SystemExit(f"Layer {name} came out empty; the FAA source format may have changed.")

    with open(os.path.join(out_dir, "cycle.json"), "w") as fh:
        json.dump({
            "cycle": cycle.isoformat(),
            "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": {k: len(v) for k, v in layers.items()},
        }, fh, indent=2)


if __name__ == "__main__":
    main()
