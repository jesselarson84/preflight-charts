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
            want = ["APT_BASE", "APT_RWY", "APT_RWY_END", "FRQ", "NAV_BASE"]
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
    data = get(DOF_CSV, binary=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.upper().endswith(".CSV"))
        text = z.read(name).decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [h.strip().upper() for h in reader.fieldnames]
    log(f"DOF {name}: columns: {', '.join(reader.fieldnames)}")
    out = []
    for r in reader:
        agl, amsl = num(col(r, "AGL")), num(col(r, "AMSL"))
        lat, lon = num(col(r, "LATDEC")), num(col(r, "LONDEC"))
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
        }, minzoom=7 if agl >= 1000 else 9 if agl >= 500 else 11))
    log(f"obstacles >= 200 ft AGL: {len(out)}")
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

    layers = {
        "airspace": airspace,
        "sua": sua_layer(sua),
        "laanc": laanc_layer(laanc),
        "airports": airports,
        "runways": runways_layer(rows, airports),
        "navaids": navaids_layer(rows),
        "obstacles": obstacles_layer(),
        "modec": modec_layer(cls),
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
