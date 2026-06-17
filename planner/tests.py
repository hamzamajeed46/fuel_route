"""
Tests for the Fuel Route Planner API.
Run with:  python manage.py test planner
"""
import json
from unittest.mock import patch
from django.test import TestCase, Client


class HaversineTests(TestCase):
    def test_nyc_to_la(self):
        from planner.services import haversine
        miles = haversine(40.7128, -74.006, 34.0522, -118.2437)
        self.assertAlmostEqual(miles, 2445, delta=20)

    def test_same_point_is_zero(self):
        from planner.services import haversine
        self.assertAlmostEqual(haversine(40.0, -74.0, 40.0, -74.0), 0.0, places=3)


class CumulativeDistancesTests(TestCase):
    def test_monotonic(self):
        from planner.services import _straight_line_route, cumulative_distances
        route = _straight_line_route(40.0, -74.0, 34.0, -118.0)
        dists = cumulative_distances(route["polyline"])
        for i in range(1, len(dists)):
            self.assertGreater(dists[i], dists[i - 1])


class StationLoadTests(TestCase):
    def test_loads_expected_count(self):
        from planner.services import load_stations
        stations = load_stations()
        self.assertEqual(len(stations), 8151)

    def test_prices_are_floats(self):
        from planner.services import load_stations
        stations = load_stations()
        for s in stations[:50]:
            self.assertIsInstance(s["price"], float)
            self.assertGreater(s["price"], 0)
            self.assertLess(s["price"], 10)


class FuelCostTests(TestCase):
    def test_single_stop(self):
        from planner.services import _calculate_fuel_cost
        stops = [{"along_route_miles": 200, "price": 3.0}]
        cost = _calculate_fuel_cost(stops, 400, 40)
        self.assertAlmostEqual(cost, 120.0, places=2)

    def test_two_stops_different_prices(self):
        from planner.services import _calculate_fuel_cost
        stops = [
            {"along_route_miles": 200, "price": 2.0},
            {"along_route_miles": 400, "price": 4.0},
        ]
        cost = _calculate_fuel_cost(stops, 600, 60)
        self.assertAlmostEqual(cost, 200.0, places=2)


class OptimizerTests(TestCase):
    def _make_route_and_stations(self, mile_markers, prices):
        from planner.services import _straight_line_route, cumulative_distances, project_station_onto_route
        start_lat, start_lon = 40.7128, -74.006
        end_lat, end_lon = 34.0522, -118.2437
        route = _straight_line_route(start_lat, start_lon, end_lat, end_lon)
        polyline = route["polyline"]
        cum_dists = cumulative_distances(polyline)
        total_miles = route["total_miles"]
        stations = []
        for miles, price in zip(mile_markers, prices):
            if miles >= total_miles:
                continue
            frac = miles / total_miles
            lon = start_lon + (end_lon - start_lon) * frac
            lat = start_lat + (end_lat - start_lat) * frac
            along = project_station_onto_route(lat, lon, polyline, cum_dists)
            if along is not None:
                stations.append({
                    "name": f"Station@{miles}",
                    "address": "Test Rd",
                    "city": "City",
                    "state": "TX",
                    "price": price,
                    "lat": lat,
                    "lon": lon,
                    "along_route_miles": along,
                })
        return stations, route

    def test_no_gap_raises(self):
        from planner.services import select_fuel_stops
        stations, route = self._make_route_and_stations([100, 800], [3.0, 3.0])
        with self.assertRaises(ValueError):
            select_fuel_stops(40.7128, -74.006, 34.0522, -118.2437, stations, route["total_miles"])

    def test_reaches_destination(self):
        from planner.services import select_fuel_stops, MAX_RANGE_MILES
        markers = list(range(150, 2450, 150))
        prices = [3.0 - (i % 5) * 0.05 for i in range(len(markers))]
        stations, route = self._make_route_and_stations(markers, prices)
        total = route["total_miles"]
        stops = select_fuel_stops(40.7128, -74.006, 34.0522, -118.2437, stations, total)
        positions = [0.0] + [s["along_route_miles"] for s in stops] + [total]
        for i in range(1, len(positions)):
            self.assertLessEqual(positions[i] - positions[i - 1], MAX_RANGE_MILES + 10)


class APITests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_missing_start(self):
        resp = self.client.post("/api/route/", data=json.dumps({"finish": "Dallas, TX"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_missing_finish(self):
        resp = self.client.post("/api/route/", data=json.dumps({"start": "Chicago, IL"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_same_location(self):
        resp = self.client.post("/api/route/",
                                data=json.dumps({"start": "Chicago, IL", "finish": "Chicago, IL"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    @patch("planner.services.geocode_location")
    @patch("planner.services.get_route")
    @patch("planner.services.batch_geocode_stations")
    def test_successful_route(self, mock_batch, mock_route, mock_geocode):
        mock_geocode.side_effect = [(41.8781, -87.6298), (32.7767, -96.797)]
        mock_route.return_value = {
            "polyline": [[-87.6298, 41.8781], [-96.797, 32.7767]],
            "total_miles": 920.0,
            "duration_hours": 14.0,
        }
        mock_batch.return_value = [
            {"name": "Station A", "address": "I-44", "city": "Tulsa", "state": "OK",
             "price": 3.1, "lat": 37.0, "lon": -93.0, "along_route_miles": 300.0},
            {"name": "Station B", "address": "US-75", "city": "Sherman", "state": "TX",
             "price": 2.9, "lat": 34.5, "lon": -96.5, "along_route_miles": 650.0},
        ]
        resp = self.client.post("/api/route/",
                                data=json.dumps({"start": "Chicago, IL", "finish": "Dallas, TX"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("fuel_stops", data)
        self.assertIn("cost_summary", data)
        self.assertIn("route", data)
        self.assertIn("map_url", data["route"])
        self.assertGreater(data["cost_summary"]["total_fuel_cost_usd"], 0)
        self.assertEqual(data["vehicle"]["max_range_miles"], 500)
        self.assertEqual(data["vehicle"]["fuel_efficiency_mpg"], 10)

    @patch("planner.services.geocode_location")
    def test_bad_location_returns_400(self, mock_geocode):
        mock_geocode.side_effect = ValueError("Could not geocode: Narnia")
        resp = self.client.post("/api/route/",
                                data=json.dumps({"start": "Narnia", "finish": "Dallas, TX"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())
