import sqlite3
import threading
import unittest

from app.dex.backtest import HistoricalMarketDataLoader
from app.dex.meteora import MeteoraDLMMProvider, MeteoraUniverse
from app.dex.snapshot_store import SnapshotStore


NOW = 1_800_000_000.0

POOL = "zxTpi4BtaWX3mgdAPoezkMD1hxx8CdeCfrqXMWvSCLX"
TOKEN_X = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"
TOKEN_Y = "So11111111111111111111111111111111111111112"


class TestMeteoraMarketDataIntegration(unittest.TestCase):

    def test_candle_close_timestamps_survive_persistence_and_loading(self):
        def fetch(url):
            if "/ohlcv?" in url:
                return {
                    "timeframe": "1h",
                    "data": [
                        {"timestamp": NOW - 7200, "close": 84.0},
                        {"timestamp": NOW - 3600, "close": 85.0},
                        # This candle has not closed at collection time.
                        {"timestamp": NOW, "close": 86.0},
                    ],
                }

            return {
                "total": 1,
                "pages": 1,
                "current_page": 1,
                "page_size": 100,
                "data": [
                    {
                        "address": POOL,
                        "name": "STONK-SOL",
                        "token_x": {
                            "address": TOKEN_X,
                            "symbol": "STONK",
                            "price": 0.044,
                        },
                        "token_y": {
                            "address": TOKEN_Y,
                            "symbol": "SOL",
                            "price": 86.0,
                        },
                        "tvl": 1_000_000,
                        "is_blacklisted": False,
                    }
                ],
            }

        provider = MeteoraDLMMProvider(
            universe=MeteoraUniverse(
                history_days=1,
                history_timeframe="1h",
            ),
            fetch=fetch,
            clock=lambda: NOW,
        )

        snapshots = provider.fetch_pools()
        self.assertEqual(len(snapshots), 1)

        snapshot = snapshots[0]

        self.assertEqual(
            snapshot.price_observations,
            (
                (NOW - 3600, 84.0),
                (NOW, 85.0),
            ),
        )

        db = sqlite3.connect(":memory:")
        try:
            store = SnapshotStore(db, threading.RLock())
            loader = HistoricalMarketDataLoader(store)

            self.assertTrue(store.save(snapshot))

            market_data = loader.load_pool(POOL)

            self.assertEqual(market_data.chain, "solana")
            self.assertEqual(market_data.token0, "STONK")
            self.assertEqual(market_data.token1, "SOL")
            self.assertEqual(
                [
                    (point.timestamp, point.price)
                    for point in market_data.observations
                ],
                [
                    (NOW - 3600, 84.0),
                    (NOW, 85.0),
                ],
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
