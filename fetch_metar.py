"""
VEDK Dumka Flying Club — Briefing Wall weather fetcher
========================================================
Pulls live METAR (and TAF) for airports around Dumka (VEDK) from the
Aviation Weather Center (AWC) Data API and writes GeoJSON/JSON that the
briefing-wall HTML reads.

v4 changes
----------
* FIXED the API endpoint.  The old script called
  `https://aviationweather.gov?ids=...` which is NOT an API route, so the
  live fetch failed every time and the map silently ran on simulated data.
  The correct METAR endpoint is `https://aviationweather.gov/api/data/metar`.
* Stations are now sorted by great-circle distance from VEDK and each
  feature carries `dist_nm` + `bearing_deg`, so the "3 nearest METAR"
  ribbon is correct even before the browser sorts.
* Added TAF fetching for the nearest TAF-issuing aerodrome (VEDO) ->
  `web/data/vedo_taf.json`, which the dashboard's TAF button can read.
* Output folder is created relative to this file, not the current working
  directory, so `python fetch_metar.py` works from anywhere.
* Hardened parsing (altimeter in hPa vs inHg, visibility "6+", missing
  fields) and clearer LIVE / FALLBACK / ERROR source reporting.

Usage
-----
    python fetch_metar.py            # fetch METAR + TAF, write GeoJSON/JSON
    python fetch_metar.py --no-taf   # METAR only

No third-party dependencies (standard library only).
"""

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

# Home base
VEDK = {"icao": "VEDK", "lat": 24.2297, "lon": 87.2697, "elev_ft": 470}

# Airports surrounding Dumka (VEDK). Order here does not matter — the
# script sorts by distance before writing.
STATIONS = ["VEDO", "VEDG", "VERC", "VECC", "VEPT"]

# Stations that issue a TAF (most GA strips do not). VEDK has no TAF, so the
# dashboard uses the nearest TAF-issuing aerodrome.
TAF_STATIONS = ["VEDO", "VECC", "VEPT"]

# Correct AWC Data API endpoints
AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
AWC_TAF_URL = "https://aviationweather.gov/api/data/taf"

# Output files, resolved relative to THIS script so cwd does not matter.
_HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_HERE, "web", "data")
OUTPUT_GEOJSON = os.path.join(OUTPUT_DIR, "regional_metar.geojson")
OUTPUT_TAF = os.path.join(OUTPUT_DIR, "vedo_taf.json")

# Fallback coordinates & elevation (ft) used when the API omits them or is
# unavailable entirely.
STATION_META = {
    "VEDO": {"name": "Deoghar Airport", "lat": 24.4450, "lon": 86.7069, "elev_ft": 814},
    "VEDG": {"name": "Kazi Nazrul Islam (Durgapur)", "lat": 23.6225, "lon": 87.2430, "elev_ft": 285},
    "VERC": {"name": "Birsa Munda (Ranchi)", "lat": 23.3143, "lon": 85.3217, "elev_ft": 2146},
    "VECC": {"name": "Netaji Subhas Chandra Bose (Kolkata)", "lat": 22.6547, "lon": 88.4467, "elev_ft": 16},
    "VEPT": {"name": "Jay Prakash Narayan (Patna)", "lat": 25.5913, "lon": 85.0880, "elev_ft": 170},
}


# ──────────────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────────────

def great_circle_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return (r * c) / 1852.0


def initial_bearing(lat1, lon1, lat2, lon2):
    """Initial bearing (degrees) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ──────────────────────────────────────────────────────────────────────
# Aviation calculations
# ──────────────────────────────────────────────────────────────────────

def calc_density_altitude(elev_ft, altim_hpa, temp_c):
    if None in (elev_ft, altim_hpa, temp_c):
        return None
    pressure_alt = elev_ft + (1013.25 - altim_hpa) * 30.0
    isa_temp = 15.0 - (1.98 * (pressure_alt / 1000.0))
    density_alt = pressure_alt + (120.0 * (temp_c - isa_temp))
    return round(density_alt)


def get_ceiling_ft(clouds):
    if not clouds:
        return None
    ceilings = [
        c.get("base") for c in clouds
        if c.get("cover") in ("BKN", "OVC", "VV") and c.get("base") is not None
    ]
    return min(ceilings) if ceilings else None


def classify_flight_rules(vis_sm, ceiling_ft):
    vis = vis_sm if vis_sm is not None else 10.0
    ceil = ceiling_ft if ceiling_ft is not None else 99999
    if ceil < 500 or vis < 1.0:
        return "LIFR"
    if ceil < 1000 or vis < 3.0:
        return "IFR"
    if ceil <= 3000 or vis <= 5.0:
        return "MVFR"
    return "VFR"


def normalize_altimeter(altim):
    """AWC returns altimeter in hPa (e.g. 1013.2) or, occasionally, inHg
    (e.g. 29.92). Normalise to hPa."""
    if altim is None:
        return None
    try:
        val = float(altim)
    except (TypeError, ValueError):
        return None
    if val < 100:  # looks like inHg
        val = val * 33.8639
    return round(val, 1)


def parse_visibility_sm(vis_raw):
    if vis_raw is None:
        return None
    try:
        return float(str(vis_raw).replace("+", "").strip())
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────────
# Networking
# ──────────────────────────────────────────────────────────────────────

def _http_get_json(url, params):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": "Mozilla/5.0 (DumkaFlyingClub-BriefingWall/4.0)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8").strip()
    if not (raw.startswith("[") or raw.startswith("{")):
        raise ValueError("Non-JSON response from AWC")
    return json.loads(raw)


def get_fallback_records():
    """Backup records if live API is unavailable, so the map still loads."""
    return [
        {"icaoId": "VEDO", "temp": 26, "dewp": 14, "altim": 1014, "wdir": 290, "wspd": 8, "visib": "6+", "rawOb": "VEDO (Simulated/Cached) 29008KT 9999 FEW030 26/14 Q1014"},
        {"icaoId": "VEDG", "temp": 27, "dewp": 15, "altim": 1013, "wdir": 310, "wspd": 10, "visib": "6+", "rawOb": "VEDG (Simulated/Cached) 31010KT 9999 SCT035 27/15 Q1013"},
        {"icaoId": "VERC", "temp": 23, "dewp": 11, "altim": 1015, "wdir": 270, "wspd": 7, "visib": "6+", "rawOb": "VERC (Simulated/Cached) 27007KT 9999 FEW040 23/11 Q1015"},
        {"icaoId": "VECC", "temp": 28, "dewp": 18, "altim": 1012, "wdir": 320, "wspd": 9, "visib": "5", "rawOb": "VECC (Simulated/Cached) 32009KT 5000 HZ FEW025 28/18 Q1012"},
        {"icaoId": "VEPT", "temp": 25, "dewp": 13, "altim": 1014, "wdir": 280, "wspd": 6, "visib": "6+", "rawOb": "VEPT (Simulated/Cached) 28006KT 9999 NSC 25/13 Q1014"},
    ]


def fetch_metar_records():
    """Return (records, source_mode)."""
    try:
        parsed = _http_get_json(AWC_METAR_URL, {
            "ids": ",".join(STATIONS),
            "format": "json",
            "hours": 3,
        })
        if isinstance(parsed, list) and parsed:
            # Keep only the single most recent METAR per station
            latest = {}
            for item in parsed:
                icao = item.get("icaoId")
                if icao and icao not in latest:
                    latest[icao] = item
            if latest:
                return list(latest.values()), "LIVE"
        print("Notice: AWC returned empty payload — using fallback data.")
    except Exception as exc:  # noqa: BLE001
        print(f"Notice: Live METAR API unavailable ({exc}). Using fallback data.")

    return get_fallback_records(), "FALLBACK"


# ──────────────────────────────────────────────────────────────────────
# Build GeoJSON
# ──────────────────────────────────────────────────────────────────────

def build_feature(item):
    icao = item.get("icaoId")
    meta = STATION_META.get(icao, {})
    lat = item.get("lat", meta.get("lat"))
    lon = item.get("lon", meta.get("lon"))
    elev_ft = item.get("elev", meta.get("elev_ft", 0))

    temp_c = item.get("temp")
    dewp_c = item.get("dewp")
    altim_hpa = normalize_altimeter(item.get("altim"))
    wdir = item.get("wdir")
    wspd = item.get("wspd")
    wgst = item.get("wgst")
    vis_sm = parse_visibility_sm(item.get("visib"))

    ceiling_ft = get_ceiling_ft(item.get("clouds", []))
    flight_cat = item.get("fltCat") or classify_flight_rules(vis_sm, ceiling_ft)
    density_alt = calc_density_altitude(elev_ft, altim_hpa, temp_c)

    dist_nm = round(great_circle_nm(VEDK["lat"], VEDK["lon"], lat, lon), 1) if lat and lon else None
    bearing = round(initial_bearing(VEDK["lat"], VEDK["lon"], lat, lon)) if lat and lon else None

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "icao": icao,
            "name": meta.get("name", icao),
            "raw_metar": item.get("rawOb", ""),
            "obs_time": item.get("reportTime", ""),
            "flight_category": flight_cat,
            "temp_c": temp_c,
            "dewpoint_c": dewp_c,
            "qnh_hpa": altim_hpa,
            "wind_dir": wdir,
            "wind_speed_kt": wspd,
            "wind_gust_kt": wgst,
            "visibility_sm": vis_sm,
            "ceiling_ft": ceiling_ft,
            "density_altitude_ft": density_alt,
            # Server-side convenience for the "3 nearest" ribbon:
            "dist_nm": dist_nm,
            "bearing_deg": bearing,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        },
    }


def fetch_and_build_geojson():
    records, source_mode = fetch_metar_records()

    features = [build_feature(item) for item in records]

    # Sort by distance to VEDK so features[0..2] are the three nearest.
    features.sort(key=lambda f: (f["properties"]["dist_nm"] is None,
                                 f["properties"]["dist_nm"] or 9e9))

    nearest3 = [f["properties"]["icao"] for f in features[:3]]

    geojson_obj = {
        "type": "FeatureCollection",
        "source": source_mode,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "home_base": VEDK["icao"],
        "nearest_three": nearest3,
        "features": features,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_GEOJSON, "w", encoding="utf-8") as f:
        json.dump(geojson_obj, f, indent=2)

    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] METAR: {len(features)} stations ({source_mode}) "
          f"-> {OUTPUT_GEOJSON}")
    print(f"           Nearest 3 to VEDK: {', '.join(nearest3)}")
    return source_mode


# ──────────────────────────────────────────────────────────────────────
# TAF (optional)
# ──────────────────────────────────────────────────────────────────────

def fetch_and_build_taf():
    """Fetch TAF for the nearest TAF-issuing aerodrome and write JSON.
    The dashboard's TAF button can read this file."""
    for icao in TAF_STATIONS:
        try:
            parsed = _http_get_json(AWC_TAF_URL, {
                "ids": icao,
                "format": "json",
            })
            if isinstance(parsed, list) and parsed:
                taf = parsed[0]
                out = {
                    "station": icao,
                    "name": STATION_META.get(icao, {}).get("name", icao),
                    "raw_taf": taf.get("rawTAF", ""),
                    "issue_time": taf.get("issueTime", ""),
                    "valid_from": taf.get("validTimeFrom", ""),
                    "valid_to": taf.get("validTimeTo", ""),
                    "source": "LIVE",
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                with open(OUTPUT_TAF, "w", encoding="utf-8") as f:
                    json.dump(out, f, indent=2)
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{stamp}] TAF:   {icao} -> {OUTPUT_TAF}")
                return
        except Exception as exc:  # noqa: BLE001
            print(f"Notice: TAF for {icao} unavailable ({exc}).")
            continue
    print("Notice: No TAF could be fetched from any station.")


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch METAR/TAF for VEDK briefing wall")
    parser.add_argument("--no-taf", action="store_true", help="Skip TAF fetch")
    args = parser.parse_args()

    fetch_and_build_geojson()
    if not args.no_taf:
        fetch_and_build_taf()


if __name__ == "__main__":
    main()
