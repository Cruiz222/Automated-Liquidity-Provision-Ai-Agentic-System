import dataclasses
import io
import json
import os
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote

from app import feedcheck
from app.config import Settings
from app.dex import PoolSelector, PoolSnapshot, load_snapshots
from app.dex import http as dexhttp
from app.dex.defillama import CHART_URL, CURRENT_URL, POOLS_URL, DefiLlamaPoolProvider, PoolUniverse
from app.dex.http import ProviderError, fetch_json
from app.fx import build_rate_service
from app.service import PlatformService
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "pools_snapshot.json"
NOW = 1_800_000_000.0
WETH, USDC, USDT_B = "0xaaaa000000000000000000000000000000000001", "0xbbbb000000000000000000000000000000000002", "0xcccc000000000000000000000000000000000003"


def row(**kw):
    base = {"pool": "id-eth-usdc", "chain": "Arbitrum", "project": "uniswap-v3", "symbol": "WETH-USDC", "poolMeta": "0.05%",
            "tvlUsd": 60e6, "apyBase": 12.0, "apyBase7d": 9.0, "volumeUsd1d": 80e6, "volumeUsd7d": 400e6,
            "underlyingTokens": [WETH, USDC], "exposure": "multi", "outlier": False}
    base.update(kw)
    return base


POOL_ROWS = [
    row(),
    row(pool="id-usdc-usdt", chain="Base", project="aerodrome-v1", symbol="USDC-USDT", poolMeta=None, tvlUsd=20e6, apyBase=3.0,
        apyBase7d=None, underlyingTokens=[USDC, USDT_B]),
    row(pool="wrong-chain", chain="Solana"),
    row(pool="wrong-project", project="some-farm"),
    row(pool="tiny", tvlUsd=1e6),
    row(pool="outlier", outlier=True),
    row(pool="single", exposure="single"),
    row(pool="three-token", symbol="WETH-USDC-USDT"),
    row(pool="meme", symbol="PEPE-WETH"),
    row(pool="no-tvl", tvlUsd=None),
    "not a dict",
]


def chart(price, points=30, start=NOW - 30 * 86400, jitter=0.0, interval=86400):
    return {
        "symbol": "X",
        "confidence": 0.99,
        "prices": [
            {"timestamp": start + i * interval + jitter, "price": price * (1 + 0.01 * (i % 3))}
            for i in range(points)
        ],
    }


def make_fetch(pools=None, low_confidence=(), fail_prices=False, fail_charts=False, calls=None):
    prices = {"arbitrum:" + WETH: 3000.0, "arbitrum:" + USDC: 1.0, "base:" + USDC: 1.0, "base:" + USDT_B: 1.0}

    def fetch(url):
        if calls is not None:
            calls.append(url)
        if url == POOLS_URL:
            if isinstance(pools, Exception):
                raise pools
            return {"status": "success", "data": POOL_ROWS if pools is None else pools}
        if url.startswith(CURRENT_URL.split("{")[0]):
            if fail_prices:
                raise ProviderError("prices down")
            ids = unquote(url.split("current/")[1]).split(",")
            return {"coins": {i: {"price": prices[i], "symbol": "X", "timestamp": NOW, "confidence": 0.5 if i in low_confidence else 0.99}
                              for i in ids if i in prices}}
        if url.startswith(CHART_URL.split("{")[0]):
            if fail_charts:
                raise ProviderError("charts down")
            ids = unquote(url.split("chart/")[1].split("?")[0]).split(",")
            query = parse_qs(url.split("?", 1)[1])
            period = query.get("period", ["1d"])[0]
            span = int(query.get("span", ["30"])[0])
            interval = 3600 if period == "1h" else 86400
            return {
                "coins": {
                    i: chart(
                        prices[i],
                        points=span,
                        start=NOW - span * interval,
                        jitter=3600.0 if period == "1d" and i.endswith(USDC) else 0.0,
                        interval=interval,
                    )
                    for i in ids if i in prices
                }
            }
        raise AssertionError(url)

    return fetch


class TestDefiLlamaProvider(unittest.TestCase):
    def provider(self, **kw):
        return DefiLlamaPoolProvider(PoolUniverse(), make_fetch(**kw), clock=lambda: NOW)

    def test_only_pools_inside_the_universe_survive(self):
        pools = self.provider().fetch_pools()
        self.assertEqual({p.pool_id for p in pools}, {"id-eth-usdc", "id-usdc-usdt"})

    def test_fields_are_parsed_conservatively(self):
        eth = next(p for p in self.provider().fetch_pools() if p.pool_id == "id-eth-usdc")
        self.assertEqual((eth.chain, eth.dex, eth.token0, eth.token1), ("arbitrum", "uniswap-v3", "WETH", "USDC"))
        self.assertEqual(eth.fee_tier_bps, 5.0)
        self.assertAlmostEqual(eth.reported_fee_apr, 0.09)                      # the lower of 12% and 9%
        self.assertEqual((eth.volume_usd_1d, eth.volume_usd_7d), (80e6, 400e6))
        self.assertEqual(eth.token_addresses, (WETH, USDC))
        self.assertEqual((eth.price0_usd, eth.price1_usd), (3000.0, 1.0))
        self.assertEqual(eth.as_of, NOW)
        self.assertIn("defillama", eth.source)

    def test_price_history_is_the_token_ratio_aligned_by_time(self):
        eth = next(p for p in self.provider().fetch_pools() if p.pool_id == "id-eth-usdc")
        self.assertEqual(len(eth.price_history), 30)                            # the one-hour offset between series is tolerated
        self.assertAlmostEqual(eth.price_history[0], 3000.0)
        self.assertAlmostEqual(eth.price_history[1], 3000.0 * 1.01 / 1.01)
        self.assertEqual(len(eth.price_observations), 30)
        self.assertEqual(eth.price_observations[0][0], NOW - 30 * 86400)
        self.assertEqual(eth.price_observations[1][0] - eth.price_observations[0][0], 86400)
        self.assertAlmostEqual(eth.price_observations[0][1], eth.price_history[0])

    def test_hourly_history_uses_requested_resolution_and_tighter_alignment(self):
        universe = PoolUniverse(
            history_period="1h",
            history_points=48,
        )
        provider = DefiLlamaPoolProvider(
            universe,
            make_fetch(),
            clock=lambda: NOW,
        )
        eth = next(
            pool for pool in provider.fetch_pools()
            if pool.pool_id == "id-eth-usdc"
        )
        self.assertEqual(len(eth.price_observations), 48)
        self.assertEqual(provider.last_report["history_observations_min"], 48)
        self.assertEqual(provider.last_report["history_interval_seconds_median"], 3600)
        self.assertAlmostEqual(provider.last_report["history_coverage_hours_min"], 47)
        self.assertEqual(
            eth.price_observations[1][0] - eth.price_observations[0][0],
            3600,
        )
        self.assertEqual(
            provider.last_report["history_period"],
            "1h",
        )
        self.assertEqual(
            provider.last_report["history_alignment_tolerance_seconds"],
            900,
        )

    def test_720_hourly_observations_are_paginated_and_deduplicated(self):
        universe = PoolUniverse(
            history_period="1h",
            history_points=720,
        )
        calls = []
        prices = {
            "arbitrum:" + WETH: 3000.0,
            "arbitrum:" + USDC: 1.0,
        }

        def fetch(url):
            if url.startswith(CHART_URL.split("{")[0]):
                calls.append(url)
                ids = unquote(url.split("chart/")[1].split("?")[0]).split(",")
                query = parse_qs(url.split("?", 1)[1])
                span = int(query["span"][0])
                end = query.get("end")

                last = NOW if end is None else float(end[0]) + 1
                first = last - (span - 1) * 3600

                return {
                    "coins": {
                        coin: {
                            "prices": [
                                {
                                    "timestamp": first + i * 3600,
                                    "price": prices[coin],
                                }
                                for i in range(span)
                            ]
                        }
                        for coin in ids if coin in prices
                    }
                }

            return make_fetch()(url)

        provider = DefiLlamaPoolProvider(
            universe,
            fetch,
            clock=lambda: NOW,
        )

        charts = provider._charts(list(prices))

        self.assertGreaterEqual(len(calls), 4)
        for coin in prices:
            self.assertEqual(len(charts[coin]), 720)
            timestamps = [timestamp for timestamp, _ in charts[coin]]
            self.assertEqual(timestamps, sorted(set(timestamps)))
            self.assertEqual(
                timestamps[-1] - timestamps[0],
                719 * 3600,
            )

    def test_chart_requests_use_small_batches(self):
        universe = PoolUniverse(
            history_period="1h",
            history_points=3,
        )
        coins = [
            f"ethereum:0x{i:040x}"
            for i in range(1, 5)
        ]
        calls = []

        def fetch(url):
            calls.append(url)
            ids = unquote(url.split("chart/")[1].split("?")[0]).split(",")
            query = parse_qs(url.split("?", 1)[1])
            self.assertEqual(int(query["span"][0]), 3)
            self.assertLessEqual(len(ids), 2)

            return {
                "coins": {
                    coin: {
                        "prices": [
                            {"timestamp": timestamp, "price": 100.0 + index}
                            for index, timestamp in enumerate((1000, 4600, 8200))
                        ]
                    }
                    for coin in ids
                }
            }

        provider = DefiLlamaPoolProvider(universe, fetch)
        charts = provider._charts(coins)

        self.assertEqual(len(calls), 2)
        self.assertEqual(set(charts), set(coins))
        self.assertTrue(all(len(charts[coin]) == 3 for coin in coins))
        self.assertEqual(provider._chart_report["batch_size"], 2)
        self.assertEqual(provider._chart_report["retrieved_coins"], 4)
        self.assertEqual(provider._chart_report["incomplete_coins"], {})

    def test_conflicting_duplicate_chart_prices_are_rejected(self):
        universe = PoolUniverse(
            history_period="1h",
            history_points=720,
        )
        calls = 0
        coin = "arbitrum:" + WETH

        def fetch(url):
            nonlocal calls
            calls += 1
            query = parse_qs(url.split("?", 1)[1])

            if "end" not in query:
                timestamps = list(range(240))
                prices = [3000.0] * 240
            else:
                timestamps = list(range(-239, 1))
                prices = [3000.0] * 240
                prices[-1] = 3100.0

            return {
                "coins": {
                    coin: {
                        "prices": [
                            {"timestamp": ts, "price": price}
                            for ts, price in zip(timestamps, prices)
                        ]
                    }
                }
            }

        provider = DefiLlamaPoolProvider(
            universe,
            fetch,
            clock=lambda: NOW,
        )

        with self.assertRaises(ProviderError):
            provider._charts([coin])

        self.assertEqual(calls, 2)

    def test_hourly_alignment_does_not_match_a_one_hour_stale_token_price(self):
        first = [(0.0, 10.0), (3600.0, 11.0), (7200.0, 12.0)]
        second = [(3600.0, 1.0), (7200.0, 1.0), (10800.0, 1.0)]
        aligned = DefiLlamaPoolProvider._ratio_observations(
            first,
            second,
            tolerance=900,
        )
        self.assertEqual(aligned, [(3600.0, 11.0), (7200.0, 12.0)])

    def test_hourly_alignment_achieves_maximum_matches_for_representative_series(self):
        def maximum_matches(first, second, tolerance):
            n, m = len(first), len(second)
            dp = [[0] * (m + 1) for _ in range(n + 1)]

            for i in range(n - 1, -1, -1):
                for j in range(m - 1, -1, -1):
                    best = max(dp[i + 1][j], dp[i][j + 1])

                    if abs(first[i][0] - second[j][0]) <= tolerance:
                        best = max(best, 1 + dp[i + 1][j + 1])

                    dp[i][j] = best

            return dp[0][0]

        cases = [
            (
                [(0.0, 10.0), (3600.0, 11.0), (7200.0, 12.0)],
                [(100.0, 1.0), (3700.0, 1.0), (7300.0, 1.0)],
                900,
            ),
            (
                [(0.0, 10.0), (3600.0, 11.0), (7200.0, 12.0)],
                [(3600.0, 1.0), (7200.0, 1.0), (10800.0, 1.0)],
                900,
            ),
            (
                [(0.0, 10.0), (3600.0, 11.0), (7200.0, 12.0)],
                [(2000.0, 1.0), (3800.0, 1.0), (10000.0, 1.0)],
                900,
            ),
        ]

        for first, second, tolerance in cases:
            with self.subTest(first=first, second=second):
                aligned = DefiLlamaPoolProvider._ratio_observations(
                    first,
                    second,
                    tolerance=tolerance,
                )

                maximum = maximum_matches(first, second, tolerance)

                self.assertEqual(len(aligned), maximum)
                self.assertEqual(
                    len({timestamp for timestamp, _ in aligned}),
                    len(aligned),
                    "A first-token observation must not be reused.",
                )

    def test_low_confidence_prices_are_not_used(self):
        pools = self.provider(low_confidence={"arbitrum:" + WETH}).fetch_pools()
        eth = next(p for p in pools if p.pool_id == "id-eth-usdc")
        self.assertIsNone(eth.price0_usd)

    def test_price_outages_degrade_to_unpriced_pools_not_guesses(self):
        pools = self.provider(fail_prices=True, fail_charts=True).fetch_pools()
        eth = next(p for p in pools if p.pool_id == "id-eth-usdc")
        self.assertEqual((eth.price0_usd, eth.price_history), (None, ()))
        evaluation = PoolSelector().evaluate(eth, NOW)
        self.assertFalse(evaluation.eligible)                                   # a volatile pool without history is excluded
        self.assertTrue(any("price history" in r for r in evaluation.reasons))

    def test_failures_that_leave_nothing_trustworthy_raise(self):
        for pools in (ProviderError("down"), [], [row(chain="Solana")]):
            with self.assertRaises(ProviderError):
                self.provider(pools=pools).fetch_pools()
        provider = DefiLlamaPoolProvider(PoolUniverse(), lambda url: {"unexpected": True}, clock=lambda: NOW)
        with self.assertRaises(ProviderError):
            provider.fetch_pools()

    def test_report_counts_each_stage(self):
        provider = self.provider()
        provider.fetch_pools()
        report = provider.last_report
        self.assertEqual((report["rows"], report["after_filter"], report["snapshots"], report["with_usd_prices"]), (11, 2, 2, 2))
        self.assertEqual(report["with_price_history"], 2)
        self.assertEqual(report["history_observations_min"], 30)
        self.assertEqual(report["history_interval_seconds_median"], 86400)
        self.assertAlmostEqual(report["history_coverage_hours_min"], 29 * 24)
        self.assertIn("uniswap-v3", report["projects_on_chosen_chains"])

    def test_pool_history_quality_reports_gaps_and_staleness(self):
        from app.dex.pool_data import PoolSnapshot

        snapshot = PoolSnapshot(
            pool_id="quality-test-pool",
            chain="ethereum",
            dex="uniswap-v3",
            token0="WETH",
            token1="USDC",
            tvl_usd=10_000_000,
            price_observations=(
                (0.0, 3000.0),
                (3600.0, 3010.0),
                (7200.0, 3020.0),
                (14400.0, 3030.0),
                (18000.0, 3040.0),
            ),
            as_of=19800.0,
        )

        quality = DefiLlamaPoolProvider._history_quality(
            snapshot,
            expected_interval_seconds=3600,
            expected_points=5,
        )

        self.assertEqual(quality["pool_id"], "quality-test-pool")
        self.assertEqual(quality["observation_count"], 5)
        self.assertAlmostEqual(quality["alignment_coverage_pct"], 100.0)
        self.assertAlmostEqual(quality["coverage_hours"], 5.0)
        self.assertEqual(quality["missing_intervals"], 1)
        self.assertAlmostEqual(quality["largest_gap_hours"], 2.0)
        self.assertAlmostEqual(
            quality["latest_observation_age_minutes"], 30.0
        )

    def test_pool_history_quality_handles_empty_history(self):
        from app.dex.pool_data import PoolSnapshot

        snapshot = PoolSnapshot(
            pool_id="empty-history-pool",
            chain="ethereum",
            dex="uniswap-v3",
            token0="WETH",
            token1="USDC",
            tvl_usd=10_000_000,
            as_of=19800.0,
        )

        quality = DefiLlamaPoolProvider._history_quality(
            snapshot,
            expected_interval_seconds=3600,
            expected_points=720,
        )

        self.assertEqual(quality["observation_count"], 0)
        self.assertEqual(quality["alignment_coverage_pct"], 0.0)
        self.assertIsNone(quality["coverage_hours"])
        self.assertEqual(quality["missing_intervals"], 0)
        self.assertIsNone(quality["largest_gap_hours"])
        self.assertIsNone(quality["latest_observation_age_minutes"])

    def test_many_coins_are_requested_in_small_batches(self):
        many = [row(pool=f"p{i}", underlyingTokens=[f"0x{i:040x}", f"0x{i + 1000:040x}"], tvlUsd=1e9 - i) for i in range(20)]
        calls = []
        DefiLlamaPoolProvider(PoolUniverse(max_pools=20), make_fetch(pools=many, calls=calls), clock=lambda: NOW).fetch_pools()
        price_calls = [c for c in calls if c.startswith("https://coins.llama.fi/prices")]
        self.assertGreaterEqual(len(price_calls), 2)
        self.assertTrue(all(len(unquote(c.split("current/")[1]).split(",")) <= 30 for c in price_calls))

    def test_current_price_report_counts_each_rejection_reason(self):
        ids = [
            "arbitrum:" + WETH,
            "arbitrum:" + USDC,
            "base:" + USDC,
            "base:" + USDT_B,
        ]

        def fetch(url):
            if url.startswith(CURRENT_URL.split("{")[0]):
                return {
                    "coins": {
                        "arbitrum:" + WETH: {
                            "price": 3000.0,
                            "timestamp": NOW,
                            "confidence": 0.99,
                        },
                        "arbitrum:" + USDC: {
                            "price": -1.0,
                            "timestamp": NOW,
                            "confidence": 0.99,
                        },
                        "base:" + USDC: {
                            "price": 1.0,
                            "timestamp": NOW - 3600,
                            "confidence": 0.99,
                        },
                    }
                }
            raise AssertionError(url)

        provider = DefiLlamaPoolProvider(
            PoolUniverse(),
            fetch,
            clock=lambda: NOW,
        )

        prices = provider._current_prices(ids)
        report = provider._current_price_report

        self.assertEqual(len(prices), 1)
        self.assertEqual(report["requested_coins"], 4)
        self.assertEqual(report["requests"], 1)
        self.assertEqual(report["returned_records"], 3)
        self.assertEqual(report["accepted_quotes"], 1)
        self.assertEqual(report["missing_records"], 1)
        self.assertEqual(report["rejected_quotes"], 2)
        self.assertEqual(
            report["rejection_reasons"],
            {
                "invalid_price": 1,
                "stale_quote": 1,
            },
        )

    def test_current_price_report_distinguishes_failed_requests(self):
        provider = self.provider(fail_prices=True)

        prices = provider._current_prices([
            "arbitrum:" + WETH,
            "arbitrum:" + USDC,
        ])
        report = provider._current_price_report

        self.assertEqual(prices, {})
        self.assertEqual(report["requested_coins"], 2)
        self.assertEqual(report["requests"], 1)
        self.assertEqual(report["failed_requests"], 1)
        self.assertEqual(report["missing_records"], 0)
        self.assertEqual(report["rejected_quotes"], 0)
        self.assertEqual(report["unresolved_due_to_request_failure"], 2)


class TestSafeFetch(unittest.TestCase):
    def fake_response(self, body: bytes):
        response = mock.MagicMock()
        response.read.side_effect = lambda n=-1: body[:n] if n >= 0 else body
        response.__enter__.return_value = response
        return mock.MagicMock(open=mock.MagicMock(return_value=response))

    def test_only_https_to_approved_hosts(self):
        for url in ("http://yields.llama.fi/pools", "https://evil.example/pools", "https://user:pw@yields.llama.fi/pools",
                    "https://yields.llama.fi:8443/pools", "ftp://yields.llama.fi/x", "https://yields.llama.fi.evil.example/pools"):
            with self.assertRaises(ProviderError, msg=url):
                fetch_json(url)

    def test_parses_json_and_caps_size(self):
        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=self.fake_response(b'{"a": 1}')):
            self.assertEqual(fetch_json("https://yields.llama.fi/pools"), {"a": 1})
        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=self.fake_response(b"x" * 100)):
            with self.assertRaises(ProviderError):
                fetch_json("https://yields.llama.fi/pools", max_bytes=50)
        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=self.fake_response(b"not json")):
            with self.assertRaises(ProviderError):
                fetch_json("https://yields.llama.fi/pools")

    def test_network_errors_and_redirects_become_provider_errors(self):
        broken = mock.MagicMock(open=mock.MagicMock(side_effect=OSError("no route")))
        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=broken):
            with self.assertRaises(ProviderError):
                fetch_json("https://yields.llama.fi/pools")
        with self.assertRaises(ProviderError):
            dexhttp._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/")


    def http_error(self, status: int, headers=None):
        return dexhttp.urllib.error.HTTPError(
            "https://yields.llama.fi/pools",
            status,
            "HTTP test error",
            headers or {},
            None,
        )

    def test_retries_503_then_returns_success(self):
        opener = mock.MagicMock()
        opener.open.side_effect = [
            self.http_error(503),
            self.fake_response(b'{"ok": true}').open.return_value,
        ]
        sleeps = []

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            result = fetch_json(
                "https://yields.llama.fi/pools",
                sleep=sleeps.append,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(sleeps, [0.5])

    def test_429_respects_retry_after_header(self):
        opener = mock.MagicMock()
        opener.open.side_effect = [
            self.http_error(429, {"Retry-After": "2"}),
            self.fake_response(b'{"ok": true}').open.return_value,
        ]
        sleeps = []

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            result = fetch_json(
                "https://yields.llama.fi/pools",
                sleep=sleeps.append,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(sleeps, [2.0])

    def test_retry_after_is_capped_at_maximum_backoff(self):
        opener = mock.MagicMock()
        opener.open.side_effect = [
            self.http_error(503, {"Retry-After": "60"}),
            self.fake_response(b'{"ok": true}').open.return_value,
        ]
        sleeps = []

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            fetch_json(
                "https://yields.llama.fi/pools",
                max_backoff_seconds=3,
                sleep=sleeps.append,
            )

        self.assertEqual(sleeps, [3.0])

    def test_exhausted_http_retries_raise_provider_error(self):
        opener = mock.MagicMock()
        opener.open.side_effect = [
            self.http_error(503),
            self.http_error(503),
            self.http_error(503),
        ]
        sleeps = []

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ProviderError, "HTTP 503 after 3 attempts"):
                fetch_json(
                    "https://yields.llama.fi/pools",
                    sleep=sleeps.append,
                )

        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_permanent_http_error_is_not_retried(self):
        opener = mock.MagicMock()
        opener.open.side_effect = self.http_error(400)
        sleeps = []

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ProviderError, "HTTP 400"):
                fetch_json(
                    "https://yields.llama.fi/pools",
                    sleep=sleeps.append,
                )

        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_network_timeout_retries_then_recovers(self):
        opener = mock.MagicMock()
        opener.open.side_effect = [
            TimeoutError("request timed out"),
            self.fake_response(b'{"recovered": true}').open.return_value,
        ]
        sleeps = []

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            result = fetch_json(
                "https://yields.llama.fi/pools",
                sleep=sleeps.append,
            )

        self.assertEqual(result, {"recovered": True})
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(sleeps, [0.5])

    def test_invalid_json_is_not_retried(self):
        opener = self.fake_response(b"not json")

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ProviderError, "invalid JSON"):
                fetch_json("https://yields.llama.fi/pools")

        self.assertEqual(opener.open.call_count, 1)


    def test_http_date_retry_after_is_respected(self):
        opener = mock.MagicMock()
        opener.open.side_effect = [
            self.http_error(503, {"Retry-After": "Tue, 10 Nov 2026 15:00:02 GMT"}),
            self.fake_response(b'{"ok": true}').open.return_value,
        ]
        sleeps = []
        fixed_now = dexhttp.datetime(
            2026, 11, 10, 15, 0, 0,
            tzinfo=dexhttp.timezone.utc,
        )

        with mock.patch.object(dexhttp.urllib.request, "build_opener", return_value=opener):
            result = fetch_json(
                "https://yields.llama.fi/pools",
                sleep=sleeps.append,
                clock=lambda: fixed_now,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(opener.open.call_count, 2)

    def test_invalid_backoff_settings_are_rejected(self):
        invalid_values = (-1, float("nan"), float("inf"), "0.5", True)

        for value in invalid_values:
            for parameter in ("backoff_seconds", "max_backoff_seconds"):
                with self.subTest(parameter=parameter, value=value):
                    with mock.patch.object(dexhttp.urllib.request, "build_opener") as build_opener:
                        with self.assertRaises(ValueError):
                            fetch_json(
                                "https://yields.llama.fi/pools",
                                **{parameter: value},
                            )
                        build_opener.assert_not_called()


class TestSelectorOnLiveShapedData(unittest.TestCase):
    def snapshot(self, **kw):
        base = dict(pool_id="p", chain="arbitrum", dex="uniswap-v2", token0="WETH", token1="USDC", tvl_usd=60e6, fee_tier_bps=30,
                    volume_usd_1d=60e6, volume_usd_7d=420e6, price_history=tuple(3000 * (1 + 0.02 * ((-1) ** i)) for i in range(30)),
                    as_of=NOW, source="t", audited=True, age_days=900)
        base.update(kw)
        return PoolSnapshot(**base)

    def test_concentrated_liquidity_pools_get_a_fee_haircut_and_a_warning(self):
        selector = PoolSelector()
        plain = selector.evaluate(self.snapshot(), NOW)
        v3 = selector.evaluate(self.snapshot(dex="uniswap-v3"), NOW)
        self.assertAlmostEqual(v3.fee_apr, plain.fee_apr * 0.5)
        self.assertTrue(any("concentrated" in w for w in v3.warnings))
        self.assertFalse(any("concentrated" in w for w in plain.warnings))

    def test_unknown_volume_is_a_warning_only_when_the_source_reports_a_fee_apr(self):
        selector = PoolSelector()
        with_apr = selector.evaluate(self.snapshot(volume_usd_1d=None, volume_usd_7d=None, fee_tier_bps=None, reported_fee_apr=0.2), NOW)
        self.assertFalse(any("volume" in r for r in with_apr.reasons))
        self.assertTrue(any("volume unknown" in w for w in with_apr.warnings))
        without = selector.evaluate(self.snapshot(volume_usd_1d=None, volume_usd_7d=None), NOW)
        self.assertTrue(any("volume" in r for r in without.reasons))


class TestRatePresets(unittest.TestCase):
    def test_preset_and_custom_sources(self):
        fetch = lambda url, timeout=5.0: {"tether": {"ngn": 1512.5}, "data": {"rate": "1511"}}
        service = build_rate_service(["coingecko-tether-ngn", "https://api.coingecko.com/x|data.rate"], spread_bps=100, min_sources=2, fetch=fetch)
        quote = service.current()
        self.assertAlmostEqual(float(quote.mid), 1511.75, places=2)
        self.assertGreater(quote.ask, quote.bid)

    def test_unknown_names_and_empty_lists_are_refused(self):
        for names in ([], ["not-a-source"]):
            with self.assertRaises(ValueError):
                build_rate_service(names)

    def test_a_failing_source_means_no_rate_not_a_made_up_one(self):
        from app.fx import RateError
        def broken(url, timeout=5.0):
            raise ProviderError("down")
        with self.assertRaises(RateError):
            build_rate_service(["coingecko-tether-ngn"], fetch=broken).current()


class TestSettings(unittest.TestCase):
    def test_data_source_and_time_scale(self):
        self.assertEqual(Settings().time_scale, 1440.0)
        self.assertEqual(Settings(data_source="live").time_scale, 1.0)
        self.assertEqual(Settings(data_source="live", sim_time_scale=60).time_scale, 60)
        with self.assertRaises(ValueError):
            Settings(data_source="mainnet")

    def test_environment_parsing(self):
        env = {"DEX_DATA": "live", "DEX_RATE_SOURCES": "coingecko-tether-ngn,https://x/y|a.b", "DEX_POOL_CHAINS": "base,arbitrum"}
        with mock.patch.dict(os.environ, env, clear=False):
            s = Settings.from_env()
        self.assertEqual((s.data_source, s.pool_chains, len(s.rate_sources)), ("live", ("base", "arbitrum"), 2))


class FlakyProvider:
    def __init__(self, clock):
        self.clock, self.fail = clock, False

    def fetch_pools(self):
        if self.fail:
            raise ProviderError("feed down")
        return [dataclasses.replace(s, as_of=self.clock()) for s in load_snapshots(EXAMPLE)[0]]


class TestServiceWithLiveData(unittest.TestCase):
    def make(self, provider, **kw):
        self.now = [NOW]
        service = PlatformService(Settings(database=":memory:", password_iterations=1000, require_pin=False, pool_refresh_seconds=0, **kw),
                                  clock=lambda: self.now[0], pool_provider=provider(lambda: self.now[0]) if callable(provider) else provider)
        self.addCleanup(service.close)
        return service

    def test_an_outage_keeps_serving_stored_data_and_reports_it(self):
        provider = FlakyProvider(lambda: self.now[0])
        service = self.make(lambda clock: provider)
        self.assertTrue(service.pools())
        provider.fail = True
        self.now[0] += 120
        self.assertTrue(service.pools())                                         # stored data still served
        self.assertFalse(service.data_status["ok"])
        self.assertIn("feed down", service.data_status["error"])
        self.now[0] += 3 * 86400
        self.assertTrue(all(not p["eligible"] for p in service.pools()))         # but it ages out of eligibility
        provider.fail = False
        self.assertTrue(all(not p["eligible"] for p in service.pools()))         # still inside the one-minute retry back-off
        self.now[0] += 61
        self.assertTrue(any(p["eligible"] for p in service.pools()))
        self.assertTrue(service.data_status["ok"])

    def test_no_data_at_all_is_an_error_not_an_empty_list(self):
        provider = FlakyProvider(lambda: self.now[0])
        provider.fail = True
        service = self.make(lambda clock: provider)
        with self.assertRaises(ProviderError):
            service.pools()

    def test_session_info_describes_the_data_source(self):
        service = self.make(lambda clock: FlakyProvider(clock), data_source="live")
        info = service.session_info()
        self.assertEqual((info["data_source"], info["sim_time_scale"]), ("live", 1.0))
        self.assertIn("paper-traded", info["pool_data"])


class PricedProvider:
    """Example pools with USD prices attached, so positions are valued the way live data values them."""

    def __init__(self, clock):
        self.clock, self.p0, self.p1 = clock, 3000.0, 1.0

    def fetch_pools(self):
        out = []
        for s in load_snapshots(EXAMPLE)[0]:
            stamp = self.clock()
            if s.pool_id == "weth-usdc-arbitrum-5bp":
                s = dataclasses.replace(
                    s, price0_usd=self.p0, price1_usd=self.p1,
                    price0_as_of=stamp, price1_as_of=stamp,
                )
            elif s.pool_id == "usdc-usdt-arbitrum-1bp":
                s = dataclasses.replace(
                    s, price0_usd=1.0, price1_usd=1.0,
                    price0_as_of=stamp, price1_as_of=stamp,
                )
            out.append(dataclasses.replace(s, as_of=stamp))
        return out


class TestPaperValuation(unittest.TestCase):
    ETH, STABLE, PRINCIPAL = "weth-usdc-arbitrum-5bp", "usdc-usdt-arbitrum-1bp", 1_000_000_000

    def setUp(self):
        self.now = [NOW]
        self.provider = PricedProvider(lambda: self.now[0])
        self.service = PlatformService(Settings(database=":memory:", password_iterations=1000, require_pin=False, pool_refresh_seconds=0,
                                                sim_time_scale=0.0, min_mark_interval_seconds=1),
                                       clock=lambda: self.now[0], pool_provider=self.provider)
        self.addCleanup(self.service.close)
        self.user = self.service.accounts.register("alice", "correct-horse-battery")["user_id"]
        self.service.ledger.deposit(self.user, "USDT", 10 * self.PRINCIPAL, "fund-0001")

    def open(self, pool):
        return self.service.open_position(self.user, pool, self.PRINCIPAL, f"open-{pool}"[:40].ljust(10, "x"))

    def value(self):
        self.now[0] += 60
        position = self.service.list_positions(self.user)[0]
        return (position["value_minor"] - position["fees_minor"]) / self.PRINCIPAL

    def test_entry_prices_are_recorded(self):
        pid = self.open(self.ETH)["position_id"]
        row = self.service.positions.get_position(pid)
        self.assertEqual((row["entry_price0_usd"], row["entry_price1_usd"]), (3000.0, 1.0))

    def test_value_follows_the_square_root_of_the_usd_price_changes(self):
        for p0, p1, expected in ((1500.0, 1.0, 0.5 ** 0.5), (6000.0, 1.0, 2 ** 0.5), (6000.0, 0.5, 1.0), (3000.0, 1.0, 1.0)):
            with self.subTest(p0=p0, p1=p1):
                self.setUp()
                self.open(self.ETH)
                self.provider.p0, self.provider.p1 = p0, p1
                self.assertAlmostEqual(self.value(), expected, places=4)

    def test_a_stable_pair_stays_at_par(self):
        self.open(self.STABLE)
        self.assertAlmostEqual(self.value(), 1.0, places=4)

    def test_market_loss_shows_up_in_pnl_not_just_impermanent_loss(self):
        self.open(self.ETH)
        self.provider.p0 = 1500.0                                              # ETH halves against USD
        self.now[0] += 60
        position = self.service.list_positions(self.user)[0]
        self.assertAlmostEqual(position["pnl_minor"] / self.PRINCIPAL, 0.5 ** 0.5 - 1, places=4)    # about -29%, far more than IL alone

    def test_fees_accrue_in_increments_from_the_last_mark(self):
        service = PlatformService(Settings(database=":memory:", password_iterations=1000, require_pin=False, pool_refresh_seconds=0,
                                           sim_time_scale=1.0, min_mark_interval_seconds=1),
                                  clock=lambda: self.now[0], pool_provider=self.provider)
        self.addCleanup(service.close)
        user = service.accounts.register("bobby", "correct-horse-battery")["user_id"]
        service.ledger.deposit(user, "USDT", 10 * self.PRINCIPAL, "fund-0002")
        service.open_position(user, self.STABLE, self.PRINCIPAL, "open-stable-bobby")
        apr = next(p for p in service.pools() if p["pool_id"] == self.STABLE)["fee_apr"]
        service.list_positions(user)
        for _ in range(4):
            self.now[0] += 900                                                  # four marks, 15 minutes apart
            fees = service.list_positions(user)[0]["fees_minor"]
        expected = self.PRINCIPAL * apr * 3600 / (365 * 86400)
        self.assertAlmostEqual(fees / expected, 1.0, delta=0.01)

    def test_values_never_exceed_the_sanity_bound(self):
        self.open(self.ETH)
        self.provider.p0 = 30_000.0                                            # a 10x move
        self.now[0] += 60
        position = self.service.list_positions(self.user)[0]
        self.assertLessEqual(position["value_minor"], 2 * self.PRINCIPAL)


class TestFeedCheck(unittest.TestCase):
    def test_report_lists_counts_pools_and_rates(self):
        lines = []
        provider = DefiLlamaPoolProvider(PoolUniverse(), make_fetch(), clock=lambda: NOW)
        rates = build_rate_service(["coingecko-tether-ngn"], fetch=lambda u, t=5.0: {"tether": {"ngn": 1500}})
        status = feedcheck.run_check(provider, rates, out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(status, 0)
        for needle in ("rows", "after filter", "with usd prices", "projects on the chosen chains", "mid 1500.00"):
            self.assertIn(needle, text)

    def test_failure_is_reported_with_a_nonzero_status(self):
        lines = []
        provider = DefiLlamaPoolProvider(PoolUniverse(), make_fetch(pools=ProviderError("HTTP 503")), clock=time.time)
        self.assertEqual(feedcheck.run_check(provider, out=lines.append), 1)
        self.assertIn("FAILED", lines[0])


if __name__ == "__main__":
    unittest.main()
