import unittest

from app.dex.backtest.market_data import (
    HistoricalMarketData,
    PricePoint,
)
from app.dex.backtest.splitting import (
    ChronologicalMarketDataSplitter,
    DataSplitError,
)


class TestChronologicalMarketDataSplitter(unittest.TestCase):
    def setUp(self):
        self.market_data = HistoricalMarketData(
            pool_id="eth-usdc-test",
            token0="ETH",
            token1="USDC",
            observations=(
                PricePoint(100, 2000),
                PricePoint(200, 2100),
                PricePoint(300, 1900),
                PricePoint(400, 2200),
                PricePoint(500, 2300),
            ),
        )
        self.splitter = ChronologicalMarketDataSplitter()

    def test_splits_observations_at_cutoff(self):
        result = self.splitter.split(self.market_data, 300)

        self.assertEqual(
            [p.timestamp for p in result.training.observations],
            [100, 200, 300],
        )
        self.assertEqual(
            [p.timestamp for p in result.testing.observations],
            [400, 500],
        )

    def test_cutoff_observation_belongs_to_training(self):
        result = self.splitter.split(self.market_data, 300)

        self.assertEqual(
            result.training.observations[-1].timestamp,
            300,
        )
        self.assertGreater(
            result.testing.observations[0].timestamp,
            300,
        )

    def test_preserves_pool_and_token_identity(self):
        result = self.splitter.split(self.market_data, 300)

        self.assertEqual(result.pool_id, "eth-usdc-test")
        self.assertEqual(result.training.token0, "ETH")
        self.assertEqual(result.training.token1, "USDC")
        self.assertEqual(result.testing.token0, "ETH")
        self.assertEqual(result.testing.token1, "USDC")

    def test_preserves_all_observations_without_duplication(self):
        result = self.splitter.split(self.market_data, 300)

        combined = (
            result.training.observations
            + result.testing.observations
        )

        self.assertEqual(combined, self.market_data.observations)

    def test_training_and_testing_do_not_overlap(self):
        result = self.splitter.split(self.market_data, 300)

        training_timestamps = {
            p.timestamp for p in result.training.observations
        }
        testing_timestamps = {
            p.timestamp for p in result.testing.observations
        }

        self.assertTrue(training_timestamps.isdisjoint(testing_timestamps))

    def test_rejects_cutoff_before_all_observations(self):
        with self.assertRaises(DataSplitError):
            self.splitter.split(self.market_data, 50)

    def test_rejects_cutoff_after_all_observations(self):
        with self.assertRaises(DataSplitError):
            self.splitter.split(self.market_data, 600)

    def test_rejects_invalid_cutoff(self):
        for cutoff in (-1, float("nan"), float("inf"), True, "300"):
            with self.subTest(cutoff=cutoff):
                with self.assertRaises(DataSplitError):
                    self.splitter.split(self.market_data, cutoff)

    def test_rejects_wrong_market_data_type(self):
        with self.assertRaises(DataSplitError):
            self.splitter.split([], 300)


if __name__ == "__main__":
    unittest.main()