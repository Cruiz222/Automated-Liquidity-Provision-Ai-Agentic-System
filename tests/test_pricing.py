import unittest
from decimal import Decimal

from app.fx import (
    CallableRateSource, HttpJsonRateSource, RateDeviation, RateError, RateQuote, RateService,
    RateUnavailable, StaleRate, StaticRateSource, json_path,
)


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestRates(unittest.TestCase):
    def test_static_source_is_two_sided(self):
        quote = StaticRateSource("1500", spread_bps=100).fetch()
        self.assertEqual(quote.mid, Decimal("1500"))
        self.assertGreater(quote.ask, quote.bid)
        self.assertEqual(round(quote.spread_bps), 100)

    def test_quote_validation(self):
        with self.assertRaises(RateError):
            RateQuote(Decimal("0"), Decimal("1"), 0, "x")
        with self.assertRaises(RateError):
            RateQuote(Decimal("1600"), Decimal("1500"), 0, "x")

    def test_service_uses_median_of_sources(self):
        clock = Clock()
        sources = [StaticRateSource(r, 0, f"s{r}", clock) for r in ("1498", "1500", "1502")]
        quote = RateService(sources, clock=clock, min_sources=3).current()
        self.assertEqual(quote.mid, Decimal("1500"))
        self.assertEqual(quote.source, "s1498+s1500+s1502")

    def test_stale_source_is_refused(self):
        clock = Clock()
        source = StaticRateSource("1500", 0, "old", lambda: 100.0)
        with self.assertRaises(StaleRate):
            RateService([source], max_age_seconds=60, clock=clock).current()

    def test_sources_that_disagree_are_refused(self):
        clock = Clock()
        sources = [StaticRateSource("1500", 0, "a", clock), StaticRateSource("1700", 0, "b", clock)]
        with self.assertRaises(RateDeviation):
            RateService(sources, max_deviation_bps=150, clock=clock).current()

    def test_failing_source_is_skipped_but_min_sources_enforced(self):
        clock = Clock()

        def boom():
            raise RuntimeError("down")

        sources = [CallableRateSource("bad", boom, clock=clock), StaticRateSource("1500", 0, "ok", clock)]
        self.assertEqual(RateService(sources, clock=clock).current().source, "ok")
        with self.assertRaises(RateUnavailable):
            RateService(sources, clock=clock, min_sources=2).current()

    def test_callable_source_accepts_pair(self):
        quote = CallableRateSource("pair", lambda: ("1490", "1510")).fetch()
        self.assertEqual((quote.bid, quote.ask), (Decimal("1490"), Decimal("1510")))

    def test_http_source_with_injected_fetch(self):
        fetch = lambda url, timeout: {"data": [{"price": "1512.5"}]}
        source = HttpJsonRateSource("fake", "https://example.test/rate", json_path("data.0.price"),
                                    spread_bps=0, fetch_json=fetch)
        self.assertEqual(source.fetch().mid, Decimal("1512.5"))

    def test_http_source_requires_https_and_reports_failures(self):
        with self.assertRaises(ValueError):
            HttpJsonRateSource("x", "http://example.test", json_path("a"))

        def fail(url, timeout):
            raise OSError("no route")

        source = HttpJsonRateSource("x", "https://example.test", json_path("a"), fetch_json=fail)
        with self.assertRaises(RateUnavailable):
            source.fetch()

    def test_json_path_missing_key(self):
        with self.assertRaises(RateError):
            json_path("a.b")({"a": {}})


if __name__ == "__main__":
    unittest.main()
