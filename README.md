# Fuel Route Planner API

A Django REST API that calculates the optimal fuel stops between two US locations,
minimising cost based on real truck-stop fuel prices.

---

## Architecture

```
POST /api/route/
      │
      ├─ 1. Nominatim (OSM) geocode START        ← free, no key
      ├─ 2. Nominatim (OSM) geocode FINISH        ← free, no key
      ├─ 3. OpenRouteService Directions (1 call)  ← free tier key
      │        returns full polyline + distance
      │
      ├─ 4. Load 8 151 stations from CSV (in-memory, loaded once)
      ├─ 5. City-level geocode stations (prebuilt cache, no API calls)
      ├─ 6. Project stations onto route polyline
      ├─ 7. Greedy look-ahead optimizer selects cheapest safe stops
      └─ 8. Return JSON with stops, cost breakdown, and Google Maps URL
```

### Geocoding strategy: Prebuilt vs. dynamic

**Station coordinates (prebuilt):** Run `python manage.py prebuild_geocache` once before 
first server start. This geocodes all ~150 unique cities in the dataset and caches them 
to `planner/city_geocache.json`. Truck stops don't move, so coordinates are static. 
Eliminates 150+ Nominatim calls per request, making startup instant.

**Fuel prices (dynamic):** Loaded fresh from `fuel_prices.csv` on every request.

**Start/finish endpoints (live):** Geocoded at request time via Nominatim (unavoidable,
since user provides arbitrary location names). Typically fast (~100ms each).

### External API call budget per request (after prebuild)
| Call | Service | Purpose |
|------|---------|---------|
| 1 | Nominatim | Geocode start location |
| 2 | Nominatim | Geocode finish location |
| 3 | OpenRouteService | Get driving route (single call) |

All station coordinates come from `city_geocache.json` (zero external calls).

### Optimization algorithm

A **greedy look-ahead** algorithm:
1. Start at mile 0 with a full 500-mile tank.
2. Find all stations reachable before the tank runs dry.
3. Among those, filter to "safe" stations — ones where there's another station
   (or the destination) within 500 miles, so we never get stranded.
4. Pick the **cheapest safe station**.
5. Repeat from that station until the destination is within range.

### Vehicle assumptions
- Max range: 500 miles per tank
- Fuel efficiency: 10 MPG
- Tank is refilled to full at each stop (conservative, ensures no stranding)

---

## Setup

### 1. Clone and install

```bash
git clone <repo>
cd fuel_route
pip install uv
uv sync
```

### 2. Get a free ORS API key

1. Go to https://openrouteservice.org/dev/#/signup
2. Sign up (free, no credit card)
3. Get your API key from the dashboard
4. Free tier: 2 000 requests/day, plenty for this use case

### 3. Set the API key

```bash
export ORS_API_KEY=your_key_here
```

Or create a `.env` file and load it:
```bash
echo "ORS_API_KEY=your_key_here" > .env
```

### 4. Prebuild the geocache (one-time setup)

```bash
python manage.py prebuild_geocache
```

This geocodes all ~150 unique cities in `fuel_prices.csv` and caches them to 
`city_geocache.json`. Takes ~3 minutes (respects Nominatim's 1 req/sec rate limit).
Coordinates are static, so this only needs to run once.

If interrupted, it resumes where it left off. Use `--force` to rebuild from scratch:
```bash
python manage.py prebuild_geocache --force
```

### 5. Run

```bash
python manage.py migrate  # only needed once, creates sqlite db
python manage.py runserver
```

---

## API Reference

### `POST /api/route/`

**Request body (JSON):**
```json
{
  "start": "New York, NY",
  "finish": "Los Angeles, CA"
}
```

Both fields accept any US city name, address, or city+state string.

**Success response (200):**
```json
{
  "start": "New York, NY",
  "finish": "Los Angeles, CA",
  "start_coordinates": { "lat": 40.7128, "lon": -74.006 },
  "finish_coordinates": { "lat": 34.0522, "lon": -118.2437 },
  "route": {
    "total_miles": 2790.5,
    "estimated_drive_hours": 40.2,
    "map_url": "https://www.google.com/maps/dir/...",
    "polyline_points": 1842
  },
  "vehicle": {
    "max_range_miles": 500,
    "fuel_efficiency_mpg": 10
  },
  "fuel_stops": [
    {
      "name": "PILOT TRAVEL CENTER #1243",
      "address": "I-8, EXIT 119 & SR-85",
      "city": "Gila Bend",
      "state": "AZ",
      "latitude": 32.947,
      "longitude": -112.717,
      "mile_marker": 487.3,
      "price_per_gallon": 3.899,
      "gallons_to_fill": 48.73,
      "segment_cost": 190.02
    }
    // ...more stops
  ],
  "cost_summary": {
    "total_miles": 2790.5,
    "total_gallons_needed": 279.05,
    "total_fuel_cost_usd": 952.14,
    "average_price_per_gallon": 3.411,
    "number_of_stops": 6
  }
}
```

**Error response (400):**
```json
{
  "error": "Both 'start' and 'finish' fields are required."
}
```

---

## Testing with curl

```bash
curl -X POST http://localhost:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Chicago, IL", "finish": "Dallas, TX"}'
```

---

## Design decisions

**Why Nominatim for geocoding?**
Completely free, no API key required, accurate for US cities.
Rate limit is 1 req/sec; we respect this with a small sleep between station
geocode calls. Start/finish geocoding is fast (2 sequential calls).

**Why OpenRouteService for routing?**
Free tier with no credit card, returns full GeoJSON polyline, covers all of
the continental USA. One call per request — satisfies the "1 call ideal" requirement.

**Why city-level geocoding for stations?**
Geocoding all 8 151 stations by street address would require 8 151 API calls.
City-level deduplication brings this down to ~600 unique (city, state) pairs,
and we only geocode cities near the route corridor. Results are cached in memory
so subsequent requests are instant.

**Why no database for stations?**
The CSV is static data. Parsing it into memory on first request takes ~50ms
and avoids a migration/seed step for the reviewer. In production, you'd load
this into a PostGIS table with a spatial index for sub-millisecond corridor queries.

**Corridor width: 5 miles**
Stations within 5 road-miles of the route are considered. Wide enough to
catch stations at highway exits, narrow enough to exclude irrelevant ones.
