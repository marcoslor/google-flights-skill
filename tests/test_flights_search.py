"""Unit tests for the flights-search.py CLI facade (offline, no network).

Covers the reverse-engineered pieces: calendar-grid response parsing
(round-trip and one-way cells), price insights/graph extraction from the
search payload, GetCalendarGrid request building, chunking, and token
handling. Live-endpoint behavior is intentionally not tested here.
"""

import importlib.util
import json
import pathlib
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location("flights_search", REPO / "skills" / "flights" / "scripts" / "flights-search.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fs = _load_script()
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _grid_response(cells):
    """Wrap grid cells the way _parse_calendar_grid expects (wrb.fr stream)."""
    inner = [None, cells]
    return json.dumps([["wrb.fr", None, json.dumps(inner)]])


class CalendarGridParsingTests(unittest.TestCase):
    def test_round_trip_cells(self):
        text = _grid_response([
            ["2026-09-15", "2026-09-20", [[None, 871], "TOK1"], 1],
            ["2026-09-16", "2026-09-21", [[12345, 700], "TOK2"], 1],
        ])
        entries = fs._parse_calendar_grid(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], {"departure": "2026-09-15", "return": "2026-09-20", "price": 871, "booking_token": "TOK1"})
        self.assertEqual(entries[1]["price"], 700)

    def test_one_way_cells(self):
        """One-leg requests answer with [date, null, [[null, price], token], 1]."""
        text = _grid_response([
            ["2026-09-10", None, [[None, 482], "OWTOK"], 1],
            ["2026-09-11", None, [[None, 439], "OWTOK2"], 1],
        ])
        entries = fs._parse_calendar_grid(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["departure"], "2026-09-10")
        self.assertIsNone(entries[0]["return"])
        self.assertEqual([e["price"] for e in entries], [482, 439])

    def test_captured_one_way_fixture(self):
        inner = json.loads((FIXTURES / "oneway_grid_inner.json").read_text())
        text = json.dumps([["wrb.fr", None, json.dumps(inner)]])
        entries = fs._parse_calendar_grid(text)
        prices = {e["departure"]: e["price"] for e in entries}
        self.assertEqual(len(entries), 11)
        self.assertEqual(prices["2026-09-10"], 482)
        self.assertEqual(prices["2026-09-13"], 439)
        for e in entries:
            self.assertIsNone(e["return"])

    def test_malformed_rows_skipped(self):
        text = _grid_response([
            "garbage",
            [123, "2026-09-20", [[None, 100]], 1],          # non-string dep
            ["2026-09-15", "2026-09-20", [["x"]], 1],        # missing price
            ["2026-09-15", "2026-09-20", [[None, "free"]], 1],  # non-numeric price
            [],
        ])
        self.assertEqual(fs._parse_calendar_grid(text), [])


def _insights_payload(current, typical, low, high):
    """Build a minimal search payload with the insights slots filled."""
    payload = [None] * 6
    payload[5] = [0, [None, current], [None, typical], [None, None], [None, low], [None, high]]
    return payload


class PriceInsightsTests(unittest.TestCase):
    def test_slots_map_to_ui_semantics(self):
        # Values captured live (GRU-JFK RT Sep 2026); UI showed "currently high".
        payload = [None] * 6
        payload[5] = [5, [None, 871], [None, 698], [None, -174], [None, 620], [None, 830]]
        out = fs.extract_price_insights(payload)
        self.assertEqual(out["current_cheapest"], 871)
        self.assertEqual(out["typical"], 698)
        self.assertEqual(out["usual_low"], 620)
        self.assertEqual(out["usual_high"], 830)
        self.assertEqual(out["current_vs_typical"], 173)

    def test_verdicts(self):
        high = fs.extract_price_insights(_insights_payload(900, 500, 400, 800))
        low = fs.extract_price_insights(_insights_payload(300, 500, 400, 800))
        normal = fs.extract_price_insights(_insights_payload(500, 500, 400, 800))
        self.assertEqual(high["verdict"], "high")
        self.assertEqual(low["verdict"], "low")
        self.assertEqual(normal["verdict"], "normal")

    def test_missing_slots_return_none(self):
        self.assertIsNone(fs.extract_price_insights(None))
        self.assertIsNone(fs.extract_price_insights([]))
        self.assertIsNone(fs.extract_price_insights([None, None, None, None, None, [5, [None, 1]]]))
        self.assertIsNone(fs.extract_price_insights([None] * 6))


class PriceGraphTests(unittest.TestCase):
    def test_graph_extraction(self):
        payload = [None] * 6
        ts = 1787329550937
        # Live shape: payload[5][10] wraps the series -> p5[10] == [ [[ts, p], ...] ]
        series = [[ts, 331], [ts, 350]]
        payload[5] = [None] * 10 + [[series]]
        graph = fs.extract_price_graph_from_payload(payload)
        self.assertEqual(graph, [{"date": "2026-08-21", "price": 331}, {"date": "2026-08-21", "price": 350}])

    def test_missing_graph_returns_none(self):
        self.assertIsNone(fs.extract_price_graph_from_payload(None))
        self.assertIsNone(fs.extract_price_graph_from_payload([None] * 6))
        short = [None] * 6
        short[5] = []
        self.assertIsNone(fs.extract_price_graph_from_payload(short))


class CalendarRequestBodyTests(unittest.TestCase):
    ARGS = dict(
        from_arg="GRU", to_arg="JFK", departure="2026-09-15",
        seat="economy", passengers={"adults": 1},
        filters={}, max_price=None, baggage={},
    )

    @staticmethod
    def _inner(body):
        from urllib.parse import parse_qs

        freq = json.loads(parse_qs(body)["f.req"][0])
        return json.loads(freq[1])

    def test_round_trip_shape(self):
        body = fs._calendar_request_body(
            return_date="2026-09-20", dep_start="2026-09-14", dep_end="2026-09-16",
            ret_start="2026-09-19", ret_end="2026-09-21", **self.ARGS,
        )
        inner = self._inner(body)
        itinerary = inner[1]
        self.assertEqual(itinerary[2], 1)                 # trip flag: round-trip
        self.assertEqual(len(itinerary[13]), 2)           # both legs
        self.assertEqual(inner[2], ["2026-09-14", "2026-09-16"])
        self.assertEqual(inner[3], ["2026-09-19", "2026-09-21"])
        leg = itinerary[13][0]
        self.assertEqual(leg[0][0][0][0], "GRU")
        self.assertEqual(leg[1][0][0][0], "JFK")
        self.assertEqual(leg[6], "2026-09-15")

    def test_one_way_shape(self):
        body = fs._calendar_request_body(
            return_date=None, dep_start="2026-08-25", dep_end="2026-10-05",
            ret_start=None, ret_end=None, **self.ARGS,
        )
        inner = self._inner(body)
        itinerary = inner[1]
        self.assertEqual(itinerary[2], 2)                 # trip flag: one-way
        self.assertEqual(len(itinerary[13]), 1)           # single leg
        self.assertIsNone(inner[3])                       # no return window


class FetchChunkingTests(unittest.TestCase):
    def _run(self, base_return):
        seen = []

        def fake_chunk(session, frm, to, dep, ret, ds, de, rs, re_, *a, **k):
            seen.append((ds, de, rs, re_))
            return [{"departure": ds, "return": ret, "price": 100, "booking_token": "t"}]

        with mock.patch.object(fs, "_calendar_grid_chunk", side_effect=fake_chunk):
            session = {"client": mock.Mock(), "f_sid": "0", "build": "b", "language": "en-US", "currency": "USD"}
            fs.fetch_calendar_grid(
                session, "GRU", "JFK",
                "2027-01-15", base_return, window=90,
                seat="economy", passengers={"adults": 1}, baggage={}, filters={},
                max_price=None, concurrency=3,
            )
        return seen

    def test_one_way_single_axis_chunks(self):
        seen = self._run(base_return=None)
        self.assertTrue(seen)
        for ds, de, rs, re_ in seen:
            self.assertIsNone(rs)
            self.assertIsNone(re_)
        # 181-day window (±90) fits one <=200-cell chunk; wider must split
        starts = sorted(s for s, _, _, _ in seen)
        total = sum((fs._parse_date(de) - fs._parse_date(ds)).days + 1 for ds, de, _, _ in seen)
        self.assertEqual(total, 181)

    def test_round_trip_rects_never_exceed_200_cells(self):
        seen = self._run(base_return="2027-02-10")
        self.assertTrue(seen)
        for ds, de, rs, re_ in seen:
            days = lambda a, b: (fs._parse_date(b) - fs._parse_date(a)).days + 1
            cells = days(ds, de) * days(rs, re_)
            self.assertLessEqual(cells, 200)


class NativeGridRouteTests(unittest.TestCase):
    def _route(self, keep_tokens):
        cells = [
            {"departure": "2026-09-15", "return": "2026-09-20", "price": 871, "booking_token": "KEEPME"},
            {"departure": "2026-09-16", "return": "2026-09-21", "price": 700, "booking_token": "DROPME"},
        ]
        with mock.patch.object(fs, "fetch_calendar_grid", return_value=cells):
            entries, _ = fs.native_grid_for_route(
                "GRU", "JFK", "2026-09-15", "2026-09-20", 2,
                currency="USD", language="", seat="economy",
                passengers={"adults": 1}, baggage={}, filters={},
                max_price=None, proxy=None, concurrency=1,
                session={"client": mock.Mock(), "f_sid": "0", "build": "b", "language": "en-US", "currency": "USD"},
                keep_tokens=keep_tokens,
            )
        return entries

    def test_tokens_dropped_by_default(self):
        entries = self._route(keep_tokens=False)
        for e in entries:
            self.assertNotIn("booking_token", e)

    def test_tokens_kept_with_flag(self):
        entries = self._route(keep_tokens=True)
        self.assertEqual([e["booking_token"] for e in entries], ["KEEPME", "DROPME"])

    def test_urls_attached(self):
        from base64 import b64decode
        from urllib.parse import urlparse, parse_qs

        entries = self._route(keep_tokens=False)
        for e in entries:
            self.assertIn("tfs=", e["url"])
            tfs = parse_qs(urlparse(e["url"]).query)["tfs"][0]
            self.assertIn(e["departure"].encode(), b64decode(tfs))


class CityEntityProtoTests(unittest.TestCase):
    def test_city_typed_url_sets_hidden_type_field(self):
        from fast_flights import FlightQuery, Passengers, create_query

        q = create_query(
            flights=[FlightQuery(date="2027-05-30", from_airport="/m/09wwlj", to_airport="/m/056_y")],
            seat="economy", trip="round-trip", passengers=Passengers(adults=1),
        )
        url = fs.city_typed_url(q, "/m/09wwlj", "/m/056_y")
        self.assertIn("tfs=", url)


class MultiAirportTests(unittest.TestCase):
    def test_split_codes(self):
        self.assertEqual(fs._split_codes("SSA,GRU"), ["SSA", "GRU"])
        self.assertEqual(fs._split_codes(" SSA , gru "), ["SSA", "gru"])
        self.assertEqual(fs._split_codes(None), [])
        self.assertEqual(fs._split_codes(""), [])

    def test_haversine_known_pairs(self):
        # SSA (-12.91, -38.33) -> GRU (-23.43, -46.47) ~1450 km
        d = fs._haversine_km(-12.913988, -38.335196, -23.435556, -46.473889)
        self.assertAlmostEqual(d, 1450, delta=60)

    def test_expand_nearby_radius_and_limit(self):
        fake = {
            "AAA": {"latitude": "0", "longitude": "0"},
            "BBB": {"latitude": "0.5", "longitude": "0"},   # ~55 km
            "CCC": {"latitude": "3", "longitude": "0"},     # ~333 km
            "DDD": {"latitude": "0.05", "longitude": "0"},  # ~5.5 km
        }
        with mock.patch.object(fs, "_fetch_airline_routes", return_value=fake):
            out, err = fs.expand_nearby(["AAA"], radius_km=100)
        self.assertIsNone(err)
        self.assertEqual(out["AAA"], ["AAA", "DDD", "BBB"])

    def test_expand_nearby_no_dataset(self):
        with mock.patch.object(fs, "_fetch_airline_routes", return_value=None):
            out, err = fs.expand_nearby(["AAA"])
        self.assertIsNone(out)
        self.assertIn("unavailable", err)

    def test_multi_tfs_roundtrip(self):
        from base64 import b64decode
        from urllib.parse import urlparse, parse_qs

        from fast_flights import FlightQuery, Passengers, create_query

        q = create_query(
            flights=[FlightQuery(date="2026-11-05", from_airport="GRU", to_airport="MAD"),
                     FlightQuery(date="2026-11-12", from_airport="MAD", to_airport="GRU")],
            seat="economy", trip="round-trip", passengers=Passengers(adults=1),
        )
        per_leg = [(["SSA", "GRU"], ["MAD"]), (["MAD"], ["GRU", "SSA"])]
        url = fs.multi_airports_tfs_url(q, per_leg)
        tfs = parse_qs(urlparse(url).query)["tfs"][0]
        C = fs._extended_proto_classes(repeat_airports=True)
        info = C["Info"]()
        info.ParseFromString(b64decode(tfs))
        self.assertEqual([a.airport for a in info.data[0].from_airport], ["SSA", "GRU"])
        self.assertEqual([a.airport for a in info.data[0].to_airport], ["MAD"])
        self.assertEqual(len(info.data[1].to_airport), 2)

    def test_multi_tfs_leg_count_mismatch_raises(self):
        from fast_flights import FlightQuery, Passengers, create_query

        q = create_query(
            flights=[FlightQuery(date="2026-11-05", from_airport="GRU", to_airport="MAD")],
            seat="economy", trip="one-way", passengers=Passengers(adults=1),
        )
        with self.assertRaises(ValueError):
            fs.multi_airports_tfs_url(q, [(["GRU"], ["MAD"]), (["MAD"], ["GRU"])])


if __name__ == "__main__":
    unittest.main()
