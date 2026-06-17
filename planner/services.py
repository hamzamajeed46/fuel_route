import csv
import json
import math
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import requests
import logging

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_RANGE_MILES = 500
MPG = 10
FUEL_CSV = Path(__file__).parent / "fuel_prices.csv"
GEOCACHE_FILE = Path(__file__).parent / "city_geocache.json"

# OpenRouteService free API — get a key at openrouteservice.org (free, no CC)
ORS_BASE = "https://api.openrouteservice.org"
ORS_API_KEY = os.getenv("ORS_API_KEY", "")  # set in environment

# Nominatim (OSM) — free geocoding, no key needed
NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
NOMINATIM_HEADERS = {"User-Agent": "FuelRouteAPI/1.0 (assessment)"}

# How close (miles) a station must be to the route to be considered
ROUTE_CORRIDOR_MILES = 5

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Haversine helpers
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2) -> float:
    """Return distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def point_to_segment_distance(px, py, ax, ay, bx, by):
    """
    Approximate perpendicular distance (degrees) from point P to segment AB.
    Good enough for corridor filtering; we convert to miles afterward.
    """
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    nearest_x = ax + t * dx
    nearest_y = ay + t * dy
    return math.hypot(px - nearest_x, py - nearest_y)


def min_distance_to_polyline_miles(lat, lon, polyline: list) -> float:
    """Return minimum distance in miles from (lat,lon) to a polyline."""
    min_dist = float("inf")
    for i in range(len(polyline) - 1):
        a = polyline[i]
        b = polyline[i + 1]
        # Quick degree-distance filter first (1 deg ≈ 69 miles)
        deg_dist = point_to_segment_distance(lon, lat, a[0], a[1], b[0], b[1])
        if deg_dist * 69 > ROUTE_CORRIDOR_MILES * 2:
            continue
        # Refine with haversine to nearest endpoint
        d = min(haversine(lat, lon, a[1], a[0]), haversine(lat, lon, b[1], b[0]))
        min_dist = min(min_dist, d)
    return min_dist


# ---------------------------------------------------------------------------
# Station distance along route
# ---------------------------------------------------------------------------

def cumulative_distances(polyline: list) -> list:
    """
    Return cumulative distances (miles) for each point in the polyline.
    polyline is list of [lon, lat] pairs (ORS format).
    """
    dists = [0.0]
    for i in range(1, len(polyline)):
        prev = polyline[i - 1]
        curr = polyline[i]
        seg = haversine(prev[1], prev[0], curr[1], curr[0])
        dists.append(dists[-1] + seg)
    return dists


def _perpendicular_distance(point, start, end) -> float:
    x0, y0 = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x0 - x1, y0 - y1)
    return abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / math.hypot(dx, dy)


def simplify_polyline(polyline: list, tolerance_miles: float = 0.1) -> list:
    """Simplify polyline using Ramer-Douglas-Peucker on lon/lat coords."""
    if len(polyline) < 3:
        return polyline

    tolerance = tolerance_miles / 69.0

    def rdp(points):
        if len(points) < 3:
            return points
        start = points[0]
        end = points[-1]
        max_dist = 0.0
        index = 0
        for i in range(1, len(points) - 1):
            dist = _perpendicular_distance(points[i], start, end)
            if dist > max_dist:
                max_dist = dist
                index = i
        if max_dist > tolerance:
            left = rdp(points[: index + 1])
            right = rdp(points[index:])
            return left[:-1] + right
        return [start, end]

    return rdp(polyline)


def project_station_onto_route(station_lat, station_lon, polyline, cum_dists) -> Optional[float]:
    """
    Return the distance-along-route (miles) where this station projects onto
    the route, or None if it's outside the corridor.
    """
    best_dist_to_route = float("inf")
    best_along = None

    for i in range(len(polyline) - 1):
        a = polyline[i]   # [lon, lat]
        b = polyline[i + 1]
        deg_dist = point_to_segment_distance(
            station_lon, station_lat, a[0], a[1], b[0], b[1]
        )
        if deg_dist * 69 > ROUTE_CORRIDOR_MILES * 2:
            continue

        seg_len = cum_dists[i + 1] - cum_dists[i]
        if seg_len == 0:
            continue

        dx = b[0] - a[0]
        dy = b[1] - a[1]
        denom = dx * dx + dy * dy
        if denom == 0:
            continue

        t = max(0.0, min(1.0, ((station_lon - a[0]) * dx + (station_lat - a[1]) * dy) / denom))
        along = cum_dists[i] + t * seg_len

        if deg_dist < best_dist_to_route:
            best_dist_to_route = deg_dist
            best_along = along

    if best_dist_to_route * 69 <= ROUTE_CORRIDOR_MILES:
        return best_along
    return None


_station_cache: Optional[list] = None
_cache_lock = threading.Lock()


def load_stations() -> list:
    """Load fuel stations from CSV, geocode address to lat/lon via Nominatim cache."""
    global _station_cache
    with _cache_lock:
        if _station_cache is not None:
            return _station_cache

        stations = []
        with open(FUEL_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stations.append({
                    "id": row["OPIS Truckstop ID"],
                    "name": row["Truckstop Name"].strip(),
                    "address": row["Address"].strip(),
                    "city": row["City"].strip(),
                    "state": row["State"].strip(),
                    "price": float(row["Retail Price"]),
                    "lat": None,
                    "lon": None,
                })

        _station_cache = stations
        return stations

_geocode_cache: dict = {}
_city_geocache: Optional[dict] = None
_geocache_lock = threading.Lock()


def load_city_geocache() -> dict:
    """
    Load city geocache from JSON file once, cache in memory (thread-safe).
    Returns empty dict if file doesn't exist (will fall back to live Nominatim).
    """
    global _city_geocache
    with _geocache_lock:
        if _city_geocache is not None:
            return _city_geocache

        start = time.perf_counter()
        _city_geocache = {}
        logger.info("Loading city geocache from file...")
        if GEOCACHE_FILE.exists():
            try:
                with open(GEOCACHE_FILE, "r", encoding="utf-8") as f:
                    _city_geocache = json.load(f)
            except Exception:
                _city_geocache = {}

        elapsed = time.perf_counter() - start
        logger.info(f"Loaded city geocache: {len(_city_geocache)} entries ({elapsed:.2f}s)")
        return _city_geocache


def geocode_location(location: str) -> tuple:
    """Geocode a place name to (lat, lon) using Nominatim. Cached."""
    if location in _geocode_cache:
        return _geocode_cache[location]

    start = time.perf_counter()
    url = f"{NOMINATIM_BASE}/search"
    params = {
        "q": location + ", USA",
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    resp = requests.get(url, params=params, headers=NOMINATIM_HEADERS, timeout=10)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not geocode: {location}")
    lat = float(results[0]["lat"])
    lon = float(results[0]["lon"])
    _geocode_cache[location] = (lat, lon)
    elapsed = time.perf_counter() - start
    logger.info(f"Geocoded '{location}' -> {lat:.6f},{lon:.6f} ({elapsed:.2f}s)")
    return lat, lon


def batch_geocode_stations(stations: list, polyline: list, cum_dists: list) -> list:
    """
    Geocode stations using prebuilt city geocache (city_geocache.json).
    For any city missing from cache, fall back to live Nominatim call.
    
    This avoids the 2+ minute startup delay by precomputing coordinates once
    (via: python manage.py prebuild_geocache).
    """
    start = time.perf_counter()
    city_geocache = load_city_geocache()
    city_cache = {}  # Local cache for this request
    needed_cities = set((s["city"], s["state"]) for s in stations)
    canadian_province_codes = {
        "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU",
        "ON", "PE", "QC", "SK", "YT",
    }

    for city, state in needed_cities:
        key = f"{city}|{state}"

        if key in city_geocache:
            coords = city_geocache[key]
            city_cache[(city, state)] = (coords["lat"], coords["lon"])
            continue

       
        try:
            country_code = "ca" if state in canadian_province_codes else "us"
            country_name = "Canada" if country_code == "ca" else "USA"
            url = f"{NOMINATIM_BASE}/search"
            params = {
                "q": f"{city}, {state}, {country_name}",
                "format": "json",
                "limit": 1,
                "countrycodes": country_code,
            }
            resp = requests.get(url, params=params, headers=NOMINATIM_HEADERS, timeout=5)
            data = resp.json()
            if data:
                coords = (float(data[0]["lat"]), float(data[0]["lon"]))
                city_cache[(city, state)] = coords
            time.sleep(0.05) 
        except Exception:
            pass

    elapsed_prep = time.perf_counter() - start
    logger.info(f"Prepared city cache for {len(city_cache)} cities ({elapsed_prep:.2f}s)")

    result = []
    proj_start = time.perf_counter()
    for s in stations:
        coords = city_cache.get((s["city"], s["state"]))
        if coords is None:
            continue
        s = dict(s)
        s["lat"] = coords[0]
        s["lon"] = coords[1]
        along = project_station_onto_route(s["lat"], s["lon"], polyline, cum_dists)
        if along is not None:
            s["along_route_miles"] = along
            result.append(s)

    projected = len(result)
    elapsed_total = time.perf_counter() - start
    elapsed_proj = time.perf_counter() - proj_start
    logger.info(f"Projected {projected} stations onto route (prep {elapsed_prep:.2f}s, proj {elapsed_proj:.2f}s, total {elapsed_total:.2f}s)")
    return result


def get_route(start_lat, start_lon, end_lat, end_lon) -> dict:
    """
    Call ORS Directions API once. Returns:
      {
        "polyline": [[lon, lat], ...],
        "total_miles": float,
        "summary": {...}
      }
    Falls back to straight-line stub if no ORS key is set (for local testing).
    """
    if not ORS_API_KEY:
        logger.info("No ORS key set — using straight-line route fallback")
        return _straight_line_route(start_lat, start_lon, end_lat, end_lon)

    url = f"{ORS_BASE}/v2/directions/driving-car/geojson"
    payload = {
        "coordinates": [[start_lon, start_lat], [end_lon, end_lat]],
        "units": "mi",
    }
    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json",
    }
    req_start = time.perf_counter()
    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    req_elapsed = time.perf_counter() - req_start
    logger.info(f"ORS directions call completed ({req_elapsed:.2f}s)")
    data = resp.json()

    feature = data["features"][0]
    coords = feature["geometry"]["coordinates"] 
    props = feature["properties"]["summary"]

    return {
        "polyline": coords,
        "total_miles": props["distance"],
        "duration_hours": props["duration"] / 3600,
    }


def _straight_line_route(start_lat, start_lon, end_lat, end_lon) -> dict:
    """Fallback: interpolate 50 points along a straight line."""
    n = 50
    coords = [
        [
            start_lon + (end_lon - start_lon) * i / (n - 1),
            start_lat + (end_lat - start_lat) * i / (n - 1),
        ]
        for i in range(n)
    ]
    total = haversine(start_lat, start_lon, end_lat, end_lon)
    return {
        "polyline": coords,
        "total_miles": total,
        "duration_hours": total / 60,
    }


def select_fuel_stops(
    start_lat, start_lon,
    end_lat, end_lon,
    route_stations: list,
    total_miles: float,
) -> list:
    """
    Greedy look-ahead algorithm:
    - Current position starts at 0 miles (start).
    - At each step, among all stations reachable within MAX_RANGE_MILES,
      pick the one with the lowest price that still keeps the next segment
      reachable (i.e., there's another station within MAX_RANGE_MILES of it,
      or the destination is within range).
    - Repeat until destination is within range.
    """

    stations = sorted(route_stations, key=lambda s: s["along_route_miles"])

    current_pos = 0.0
    stops = []

    while current_pos + MAX_RANGE_MILES < total_miles:
        reachable = [
            s for s in stations
            if current_pos < s["along_route_miles"] <= current_pos + MAX_RANGE_MILES
        ]

        if not reachable:
            raise ValueError(
                f"No fuel station found within {MAX_RANGE_MILES} miles of "
                f"mile marker {current_pos:.0f}. Route may pass through "
                f"an area with no stations in the dataset."
            )

        def is_safe(station):
            p = station["along_route_miles"]
            remaining = total_miles - p
            if remaining <= MAX_RANGE_MILES:
                return True 

            return any(
                p < s["along_route_miles"] <= p + MAX_RANGE_MILES
                for s in stations
            )

        safe_reachable = [s for s in reachable if is_safe(s)]
        if not safe_reachable:
            safe_reachable = reachable 
        best = min(safe_reachable, key=lambda s: s["price"])
        stops.append(best)
        current_pos = best["along_route_miles"]

    return stops

def plan_route(start: str, finish: str) -> dict:
    """
    Full pipeline:
      1. Geocode start & finish (Nominatim, 2 calls)
      2. Get driving route (ORS, 1 call)  ← the only routing API call
      3. Load station data from CSV (in-memory)
      4. Geocode stations at city level and project onto route
      5. Run greedy optimizer
      6. Build response
    """
   
    start_lat, start_lon = geocode_location(start)
    end_lat, end_lon = geocode_location(finish)

    
    route = get_route(start_lat, start_lon, end_lat, end_lon)
    polyline = route["polyline"]
    total_miles = route["total_miles"]

    simplified_polyline = simplify_polyline(polyline, tolerance_miles=0.1)
    logger.info(
        f"Simplified route polyline from {len(polyline)} to {len(simplified_polyline)} points"
    )
    cum_dists = cumulative_distances(simplified_polyline)

    all_stations = load_stations()
    route_stations = batch_geocode_stations(all_stations, simplified_polyline, cum_dists)

    if not route_stations:
        raise ValueError("No fuel stations found along this route.")

    
    stops = select_fuel_stops(
        start_lat, start_lon,
        end_lat, end_lon,
        route_stations,
        total_miles,
    )


    total_gallons = total_miles / MPG
    fuel_cost = _calculate_fuel_cost(stops, total_miles, total_gallons)

    stop_details = []
    prev_pos = 0.0
    for stop in stops:
        seg_miles = stop["along_route_miles"] - prev_pos
        seg_gallons = seg_miles / MPG
        stop_details.append({
            "name": stop["name"],
            "address": stop["address"],
            "city": stop["city"],
            "state": stop["state"],
            "latitude": stop["lat"],
            "longitude": stop["lon"],
            "mile_marker": round(stop["along_route_miles"], 1),
            "price_per_gallon": round(stop["price"], 3),
            "gallons_to_fill": round(seg_gallons, 2),
            "segment_cost": round(seg_gallons * stop["price"], 2),
        })
        prev_pos = stop["along_route_miles"]

    map_url = _build_map_url(start, finish, stops)

    return {
        "start": start,
        "finish": finish,
        "start_coordinates": {"lat": start_lat, "lon": start_lon},
        "finish_coordinates": {"lat": end_lat, "lon": end_lon},
        "route": {
            "total_miles": round(total_miles, 1),
            "estimated_drive_hours": round(route.get("duration_hours", total_miles / 60), 1),
            "map_url": map_url,
            "polyline_points": len(polyline),
        },
        "vehicle": {
            "max_range_miles": MAX_RANGE_MILES,
            "fuel_efficiency_mpg": MPG,
        },
        "fuel_stops": stop_details,
        "cost_summary": {
            "total_miles": round(total_miles, 1),
            "total_gallons_needed": round(total_gallons, 2),
            "total_fuel_cost_usd": round(fuel_cost, 2),
            "average_price_per_gallon": round(fuel_cost / total_gallons, 3) if total_gallons else 0,
            "number_of_stops": len(stops),
        },
    }


def _calculate_fuel_cost(stops: list, total_miles: float, total_gallons: float) -> float:
    """
    Calculate total fuel cost based on how many gallons are bought at each stop.
    Last segment (from last stop to destination) is fuelled at the last stop's price.
    """
    cost = 0.0
    prev_pos = 0.0
    for stop in stops:
        seg_miles = stop["along_route_miles"] - prev_pos
        seg_gallons = seg_miles / MPG
        cost += seg_gallons * stop["price"]
        prev_pos = stop["along_route_miles"]
    remaining_miles = total_miles - prev_pos
    if remaining_miles > 0 and stops:
        cost += (remaining_miles / MPG) * stops[-1]["price"]
    return cost


def _build_map_url(start: str, finish: str, stops: list) -> str:
    """Build a Google Maps URL with waypoints (no API key required)."""
    import urllib.parse
    base = "https://www.google.com/maps/dir/"
    parts = [urllib.parse.quote_plus(start)]
    for s in stops:
        waypoint = f"{s['lat']},{s['lon']}"
        parts.append(urllib.parse.quote_plus(waypoint))
    parts.append(urllib.parse.quote_plus(finish))
    return base + "/".join(parts)
