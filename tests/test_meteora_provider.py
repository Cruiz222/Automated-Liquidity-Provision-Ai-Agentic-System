import math
import unittest
from urllib.parse import parse_qs, urlparse

from app.dex.http import ProviderError
from app.dex.meteora import (
    POOLS_URL,
    MeteoraDLMMProvider,
    MeteoraUniverse,
)


NOW = 1_800_000_000.0

# Solana addresses are case-sensitive. Preserve these exact values.
POOL_A = "zxTpi4BtaWX3mgdAPoezkMD1hxx8CdeCfrqXMWvSCLX"
TOKEN_X = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"
TOKEN_Y = "So11111111111111111111111111111111111111112"


def meteora_pool(**overrides):
    """A minimal representative Meteora DLMM pool response."""
    pool = {
        "address": POOL_A,
        "name": "STONK-SOL",
        "token_x": {
            "address": TOKEN_X,
            "symbol": "STONK",
            "price": 0.044028944323553226,
        },
        "token_y": {
            "address": TOKEN_Y,
            "symbol": "SOL",
            "price": 86.9069068135544,
        },
        "tvl": 1_476_996.71229917,
        "is_blacklisted": False,
    }
    pool.update(overrides)
    return pool


def response(data, page=1, page_size=100, pages=1):
    return {
        "total": len(data),
        "pages": pages,
        "current_page": page,
        "page_size": page_size,
        "data": data,
    }


class TestMeteoraUniverse(unittest.TestCase):

    def test_rejects_non_positive_minimum_tvl(self):
        with self.assertRaises(ValueError):
            MeteoraUniverse(min_tvl_usd=0)

    def test_rejects_invalid_pool_limit(self):
        with self.assertRaises(ValueError):
            MeteoraUniverse(max_pools=0)

    def test_rejects_invalid_page_size(self):
        with self.assertRaises(ValueError):
            MeteoraUniverse(page_size=1001)

    def test_rejects_invalid_page_limit(self):
        with self.assertRaises(ValueError):
            MeteoraUniverse(max_pages=0)


    def test_rejects_non_positive_history_days(self):
        for value in (0, -1):
            with self.subTest(history_days=value):
                with self.assertRaises(ValueError):
                    MeteoraUniverse(history_days=value)

    def test_rejects_invalid_history_timeframe(self):
        for value in ("", "15m", "1d", "invalid"):
            with self.subTest(history_timeframe=value):
                with self.assertRaises(ValueError):
                    MeteoraUniverse(history_timeframe=value)


class TestMeteoraDLMMProvider(unittest.TestCase):

    def provider(self, fetch, **universe_options):
        universe = MeteoraUniverse(**universe_options)
        return MeteoraDLMMProvider(
            universe=universe,
            fetch=fetch,
            clock=lambda: NOW,
        )

    def test_maps_valid_pool_to_snapshot(self):
        def fetch(url):
            return response([meteora_pool()])

        pools = self.provider(fetch).fetch_pools()

        self.assertEqual(len(pools), 1)

        pool = pools[0]

        self.assertEqual(pool.pool_id, POOL_A)
        self.assertEqual(pool.chain, "solana")
        self.assertEqual(pool.dex, "meteora-dlmm")
        self.assertEqual((pool.token0, pool.token1), ("STONK", "SOL"))
        self.assertEqual(pool.token_addresses, (TOKEN_X, TOKEN_Y))

        self.assertAlmostEqual(pool.tvl_usd, 1_476_996.71229917)
        self.assertAlmostEqual(pool.price0_usd, 0.044028944323553226)
        self.assertAlmostEqual(pool.price1_usd, 86.9069068135544)

        self.assertEqual(pool.as_of, NOW)
        self.assertEqual(pool.source, "meteora-dlmm-api")

    def test_preserves_case_sensitive_solana_addresses(self):
        def fetch(url):
            return response([meteora_pool()])

        pool = self.provider(fetch).fetch_pools()[0]

        self.assertEqual(pool.pool_id, POOL_A)
        self.assertEqual(pool.token_addresses[0], TOKEN_X)
        self.assertEqual(pool.token_addresses[1], TOKEN_Y)

    def test_does_not_map_unverified_volume_or_yield_fields(self):
        record = meteora_pool(
            volume={"24h": 10_470_492.66},
            apr=1.7573215,
            apy=57_638.88,
            dynamic_fee_pct=0.001,
        )

        def fetch(url):
            return response([record])

        pool = self.provider(fetch).fetch_pools()[0]

        self.assertIsNone(pool.volume_usd_1d)
        self.assertIsNone(pool.volume_usd_7d)
        self.assertIsNone(pool.reported_fee_apr)
        self.assertIsNone(pool.fee_tier_bps)

    def test_excludes_blacklisted_pools_by_default(self):
        records = [
            meteora_pool(),
            meteora_pool(
                address="AnotherPoolAddress",
                is_blacklisted=True,
            ),
        ]

        def fetch(url):
            return response(records)

        pools = self.provider(fetch).fetch_pools()

        self.assertEqual([pool.pool_id for pool in pools], [POOL_A])

    def test_can_include_blacklisted_pools_when_explicitly_enabled(self):
        record = meteora_pool(is_blacklisted=True)

        def fetch(url):
            return response([record])

        pools = self.provider(
            fetch,
            include_blacklisted=True,
        ).fetch_pools()

        self.assertEqual(len(pools), 1)

    def test_excludes_pools_below_minimum_tvl(self):
        records = [
            meteora_pool(),
            meteora_pool(
                address="SmallPoolAddress",
                tvl=99_999.99,
            ),
        ]

        def fetch(url):
            return response(records)

        pools = self.provider(fetch).fetch_pools()

        self.assertEqual([pool.pool_id for pool in pools], [POOL_A])

    def test_rejects_invalid_tvl_values_without_creating_snapshots(self):
        invalid_values = [
            None,
            True,
            False,
            0,
            -1,
            float("nan"),
            float("inf"),
            "100000",
        ]

        for value in invalid_values:
            with self.subTest(tvl=value):
                def fetch(url):
                    return response([
                        meteora_pool(
                            address="InvalidTVLPool",
                            tvl=value,
                        )
                    ])

                pools = self.provider(fetch).fetch_pools()

                self.assertEqual(pools, [])

    def test_excludes_records_with_invalid_token_data(self):
        invalid_records = [
            meteora_pool(token_x=None),
            meteora_pool(token_y={"address": TOKEN_Y}),
            meteora_pool(token_x={
                "address": TOKEN_X,
                "symbol": "",
                "price": 1.0,
            }),
            meteora_pool(token_y={
                "address": TOKEN_Y,
                "symbol": "SOL",
                "price": float("nan"),
            }),
        ]

        def fetch(url):
            return response(invalid_records)

        self.assertEqual(self.provider(fetch).fetch_pools(), [])

    def test_rejects_invalid_root_response(self):
        invalid_payloads = [
            None,
            [],
            {},
            {"data": "not a list"},
            {"data": [], "pages": "one"},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                def fetch(url):
                    return payload

                with self.assertRaises(ProviderError):
                    self.provider(fetch).fetch_pools()

    def test_invalid_record_does_not_suppress_later_valid_duplicate(self):
        invalid_record = meteora_pool(
            token_x={
                "address": TOKEN_X,
                "symbol": "",
                "price": 0.044,
            }
        )

        valid_record = meteora_pool()

        def fetch(url):
            return response([invalid_record, valid_record])

        pools = self.provider(fetch).fetch_pools()

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0].pool_id, POOL_A)
        self.assertEqual(pools[0].token0, "STONK")

    def test_rejects_unexpected_response_page_size(self):
        def fetch(url):
            return response(
                [meteora_pool()],
                page=1,
                page_size=99,
                pages=1,
            )

        with self.assertRaises(ProviderError):
            self.provider(fetch, page_size=100).fetch_pools()

    def test_rejects_total_pool_count_changing_between_pages(self):
        def fetch(url):
            query = parse_qs(urlparse(url).query)
            page = int(query["page"][0])

            if page == 1:
                payload = response(
                    [meteora_pool(address="PoolOne")],
                    page=1,
                    page_size=1,
                    pages=2,
                )
                payload["total"] = 2
                return payload

            payload = response(
                [meteora_pool(address="PoolTwo")],
                page=2,
                page_size=1,
                pages=2,
            )
            payload["total"] = 3
            return payload

        with self.assertRaises(ProviderError):
            self.provider(
                fetch,
                page_size=1,
                max_pages=2,
            ).fetch_pools()

    def test_paginates_until_pool_limit_is_reached(self):
        calls = []

        def fetch(url):
            query = parse_qs(urlparse(url).query)
            page = int(query["page"][0])
            calls.append(page)

            if page == 1:
                return response(
                    [meteora_pool(address="PoolOne")],
                    page=1,
                    page_size=1,
                    pages=3,
                )

            if page == 2:
                return response(
                    [meteora_pool(address="PoolTwo")],
                    page=2,
                    page_size=1,
                    pages=3,
                )

            return response(
                [meteora_pool(address="PoolThree")],
                page=3,
                page_size=1,
                pages=3,
            )

        pools = self.provider(
            fetch,
            max_pools=2,
            page_size=1,
            max_pages=10,
        ).fetch_pools()

        self.assertEqual(
            [pool.pool_id for pool in pools],
            ["PoolOne", "PoolTwo"],
        )
        self.assertEqual(calls, [1, 2])

    def test_never_requests_more_than_max_pages(self):
        calls = []

        def fetch(url):
            page = int(parse_qs(urlparse(url).query)["page"][0])
            calls.append(page)

            return response(
                [
                    meteora_pool(
                        address=f"Pool{page}",
                        tvl=1_000_000,
                    )
                ],
                page=page,
                page_size=1,
                pages=100,
            )

        pools = self.provider(
            fetch,
            min_tvl_usd=100_000,
            max_pools=100,
            page_size=1,
            max_pages=3,
        ).fetch_pools()

        self.assertEqual(len(pools), 3)
        self.assertEqual(calls, [1, 2, 3])

    def test_rejects_non_progressing_pagination(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return response(
                [meteora_pool()],
                page=1,
                page_size=1,
                pages=3,
            )

        provider = self.provider(
            fetch,
            max_pools=10,
            page_size=1,
            max_pages=3,
        )

        with self.assertRaises(ProviderError):
            provider.fetch_pools()

        self.assertEqual(len(calls), 2)

    def test_propagates_provider_errors(self):
        def fetch(url):
            raise ProviderError("Meteora API unavailable")

        with self.assertRaises(ProviderError):
            self.provider(fetch).fetch_pools()

    def test_report_records_discovery_counts(self):
        records = [
            meteora_pool(),
            meteora_pool(
                address="BlacklistedPool",
                is_blacklisted=True,
            ),
            meteora_pool(
                address="SmallPool",
                tvl=10_000,
            ),
        ]

        def fetch(url):
            return response(records)

        provider = self.provider(fetch)
        provider.fetch_pools()

        self.assertIsInstance(provider.last_report, dict)
        self.assertEqual(provider.last_report["pages_requested"], 1)
        self.assertEqual(provider.last_report["pools_accepted"], 1)


    def test_enriches_pool_with_timestamped_ohlcv_closes(self):
        calls = []

        def fetch(url):
            calls.append(url)

            if "/ohlcv?" in url:
                return {
                    "timeframe": "1h",
                    "data": [
                        {"timestamp": NOW - 3600, "close": 85.0},
                        {"timestamp": NOW, "close": 86.0},
                    ],
                }

            return response([meteora_pool()])

        pool = self.provider(
            fetch,
            history_days=1,
            history_timeframe="1h",
        ).fetch_pools()[0]

        self.assertEqual(len(calls), 2)

        history_query = parse_qs(urlparse(calls[1]).query)
        self.assertEqual(history_query["timeframe"], ["1h"])
        self.assertEqual(
            history_query["start_time"],
            [str(int(NOW) - 86400)],
        )
        self.assertEqual(history_query["end_time"], [str(int(NOW))])

        self.assertEqual(pool.price_history, (85.0, 86.0))
        self.assertEqual(
            pool.price_observations,
            (
                (NOW - 3600, 85.0),
                (NOW, 86.0),
            ),
        )

    def test_empty_ohlcv_history_preserves_discovered_pool(self):
        def fetch(url):
            if "/ohlcv?" in url:
                return {"timeframe": "1h", "data": []}

            return response([meteora_pool()])

        provider = self.provider(fetch)
        pools = provider.fetch_pools()

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0].pool_id, POOL_A)
        self.assertEqual(pools[0].price_history, ())
        self.assertEqual(pools[0].price_observations, ())

        history = provider.last_report["history"]
        self.assertEqual(history["requests"], 1)
        self.assertEqual(history["pools_with_history"], 0)
        self.assertEqual(history["pools_without_history"], 1)
        self.assertEqual(history["failures"], [])

    def test_ohlcv_request_failure_preserves_discovered_pool(self):
        def fetch(url):
            if "/ohlcv?" in url:
                raise ProviderError("OHLCV endpoint unavailable")

            return response([meteora_pool()])

        provider = self.provider(fetch)
        pools = provider.fetch_pools()

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0].pool_id, POOL_A)
        self.assertEqual(pools[0].price_observations, ())

        history = provider.last_report["history"]
        self.assertEqual(history["pools_with_history"], 0)
        self.assertEqual(history["pools_without_history"], 1)
        self.assertEqual(len(history["failures"]), 1)
        self.assertEqual(history["failures"][0]["pool_id"], POOL_A)
        self.assertIn(
            "OHLCV endpoint unavailable",
            history["failures"][0]["error"],
        )

    def test_rejects_malformed_ohlcv_candles_without_losing_pool(self):
        valid = {"timestamp": NOW - 3600, "close": 85.0}

        invalid_histories = [
            [None],
            [{"close": 85.0}],
            [{"timestamp": float("nan"), "close": 85.0}],
            [{"timestamp": NOW, "close": float("nan")}],
            [{"timestamp": NOW, "close": 0}],
            [{"timestamp": NOW, "close": -1}],
            [{"timestamp": NOW + 1, "close": 85.0}],
            [
                {"timestamp": NOW, "close": 85.0},
                {"timestamp": NOW, "close": 86.0},
            ],
            [
                {"timestamp": NOW, "close": 85.0},
                {"timestamp": NOW - 3600, "close": 86.0},
            ],
        ]

        for candles in invalid_histories:
            with self.subTest(candles=candles):
                def fetch(url):
                    if "/ohlcv?" in url:
                        return {"timeframe": "1h", "data": candles}

                    return response([meteora_pool()])

                provider = self.provider(fetch)
                pools = provider.fetch_pools()

                self.assertEqual(len(pools), 1)
                self.assertEqual(pools[0].pool_id, POOL_A)
                self.assertEqual(pools[0].price_observations, ())
                self.assertEqual(
                    provider.last_report["history"]["pools_without_history"],
                    1,
                )
                self.assertEqual(
                    len(provider.last_report["history"]["failures"]),
                    1,
                )

    def test_rejects_ohlcv_timeframe_mismatch(self):
        def fetch(url):
            if "/ohlcv?" in url:
                return {
                    "timeframe": "1d",
                    "data": [{"timestamp": NOW, "close": 85.0}],
                }

            return response([meteora_pool()])

        provider = self.provider(
            fetch,
            history_timeframe="1h",
        )
        pools = provider.fetch_pools()

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0].price_observations, ())
        self.assertEqual(
            len(provider.last_report["history"]["failures"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
