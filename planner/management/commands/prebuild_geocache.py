import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from django.core.management.base import BaseCommand

FUEL_CSV = Path(__file__).parent.parent.parent / "fuel_prices.csv"
GEOCACHE_FILE = Path(__file__).parent.parent.parent / "city_geocache.json"

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
NOMINATIM_HEADERS = {"User-Agent": "FuelRouteAPI/1.0 (assessment)"}
CANADIAN_PROVINCE_CODES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU",
    "ON", "PE", "QC", "SK", "YT",
}

MAX_RETRIES = 3
BACKOFF_SLEEP = 3.0
BATCH_SIZE = 5
SAVE_INTERVAL = 10  
BATCH_DELAY = 1.0 


def _city_query_variants(city: str) -> list[str]:
    city = city.strip()
    variants = [city]
    title = city.title()
    if title != city:
        variants.append(title)

    if city.upper().startswith("MC "):
        rest = city[3:].strip().title().replace(" ", "")
        if rest:
            variants.append("Mc" + rest)
            variants.append("Mc " + city[3:].strip().title())

    return [v for i, v in enumerate(variants) if v not in variants[:i]]


class Command(BaseCommand):
    help = "Prebuild geocache for all cities in fuel_prices.csv using Nominatim"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Rebuild entire cache instead of resuming from existing",
        )

    def geocode_city(self, city: str, state: str) -> tuple:
        """Geocode a single city. Returns (key, coords) or (key, None) on failure."""
        key = f"{city}|{state}"
        url = f"{NOMINATIM_BASE}/search"
        country_code = "ca" if state in CANADIAN_PROVINCE_CODES else "us"
        country_name = "Canada" if country_code == "ca" else "USA"
        variants = _city_query_variants(city)

        for variant in variants:
            params = {
                "q": f"{variant}, {state}, {country_name}",
                "format": "json",
                "limit": 1,
                "countrycodes": country_code,
            }
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = requests.get(
                        url, params=params, headers=NOMINATIM_HEADERS, timeout=10
                    )
                    if resp.status_code == 429:
                        if attempt < MAX_RETRIES:
                            time.sleep(BACKOFF_SLEEP)
                            continue
                        return key, None, "rate_limited"

                    resp.raise_for_status()
                    data = resp.json()
                    if data:
                        coords = {
                            "lat": float(data[0]["lat"]),
                            "lon": float(data[0]["lon"]),
                        }
                        return key, coords, "success"
                    break
                except requests.RequestException as e:
                    if attempt < MAX_RETRIES:
                        time.sleep(BACKOFF_SLEEP)
                        continue
                    return key, None, f"error: {str(e)}"

        for variant in variants:
            params = {
                "q": f"{variant}, {state}",
                "format": "json",
                "limit": 1,
            }
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = requests.get(
                        url, params=params, headers=NOMINATIM_HEADERS, timeout=10
                    )
                    if resp.status_code == 429:
                        if attempt < MAX_RETRIES:
                            time.sleep(BACKOFF_SLEEP)
                            continue
                        return key, None, "rate_limited"

                    resp.raise_for_status()
                    data = resp.json()
                    if data:
                        coords = {
                            "lat": float(data[0]["lat"]),
                            "lon": float(data[0]["lon"]),
                        }
                        return key, coords, "success"
                    break
                except requests.RequestException as e:
                    if attempt < MAX_RETRIES:
                        time.sleep(BACKOFF_SLEEP)
                        continue
                    return key, None, f"error: {str(e)}"

        return key, None, "no_result"

    def handle(self, *args, **options):
        # Load existing cache if resuming
        geocache = {}
        if GEOCACHE_FILE.exists() and not options["force"]:
            with open(GEOCACHE_FILE, "r", encoding="utf-8") as f:
                geocache = json.load(f)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Loaded {len(geocache)} existing entries. Resuming..."
                )
            )
        else:
            if options["force"]:
                self.stdout.write("--force flag set. Rebuilding entire cache...")
            self.stdout.write("Starting fresh geocache build...")

        cities = set()
        with open(FUEL_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                city = row["City"].strip()
                state = row["State"].strip()
                cities.add((city, state))

        self.stdout.write(f"Found {len(cities)} unique cities in CSV\n")

        cities_to_geocode = [
            (c, s) for c, s in sorted(cities) if f"{c}|{s}" not in geocache
        ]

        if not cities_to_geocode:
            self.stdout.write(
                self.style.SUCCESS("All cities already cached! Nothing to do.")
            )
            return

        self.stdout.write(f"Geocoding {len(cities_to_geocode)} remaining cities...\n")

        stats = {"geocoded": 0, "failed": 0}

        total = len(cities_to_geocode)
        for batch_start in range(0, total, BATCH_SIZE):
            batch = cities_to_geocode[batch_start:batch_start + BATCH_SIZE]
            self.stdout.write(
                f"Processing batch {batch_start // BATCH_SIZE + 1} "
                f"of {((total - 1) // BATCH_SIZE) + 1} ({len(batch)} cities)..."
            )

            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = {}
                for batch_idx, (city, state) in enumerate(batch):
                    future = executor.submit(self.geocode_city, city, state)
                    futures[future] = (city, state, batch_start + batch_idx + 1)

                for future in as_completed(futures):
                    city, state, idx = futures[future]
                    key, coords, status = future.result()

                    if status == "success":
                        self.stdout.write(
                            f"[{idx}/{total}] {key}",
                            ending=" ",
                        )
                        self.stdout.write(self.style.SUCCESS("✓"))
                        stats["geocoded"] += 1
                        geocache[key] = coords
                    else:
                        self.stdout.write(
                            f"[{idx}/{total}] {key} ({status})",
                            ending=" ",
                        )
                        self.stdout.write(self.style.ERROR("✗"))
                        stats["failed"] += 1

            if (batch_start + len(batch)) % SAVE_INTERVAL == 0:
                with open(GEOCACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(geocache, f, indent=2)
                self.stdout.write(
                    self.style.WARNING(
                        f"\n Saved progress ({len(geocache)} entries)...\n"
                    )
                )

            time.sleep(BATCH_DELAY)

        # Final save
        with open(GEOCACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(geocache, f, indent=2)

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Geocache saved to {GEOCACHE_FILE.name}"
            )
        )
        self.stdout.write(f"  Total entries: {len(geocache)}")
        self.stdout.write(f"  Newly geocoded: {stats['geocoded']}")
        self.stdout.write(f"  Failed: {stats['failed']}")
        self.stdout.write("=" * 70)
