"""Chronological train/test splitting for DEX historical market data."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.dex.backtest.market_data import HistoricalMarketData


class DataSplitError(ValueError):
    """Invalid chronological market-data split."""


@dataclass(frozen=True, slots=True)
class HistoricalMarketDataSplit:
    """Disjoint chronological training and testing data for one pool."""

    pool_id: str
    cutoff_timestamp: float
    training: HistoricalMarketData
    testing: HistoricalMarketData

    def __post_init__(self) -> None:
        if not isinstance(self.training, HistoricalMarketData):
            raise DataSplitError("training must be HistoricalMarketData")

        if not isinstance(self.testing, HistoricalMarketData):
            raise DataSplitError("testing must be HistoricalMarketData")

        if self.training.pool_id != self.pool_id:
            raise DataSplitError("training pool_id does not match split")

        if self.testing.pool_id != self.pool_id:
            raise DataSplitError("testing pool_id does not match split")

        if self.training.chain != self.testing.chain:
            raise DataSplitError("training and testing chains differ")

        if (
            self.training.token0.upper() != self.testing.token0.upper()
            or self.training.token1.upper() != self.testing.token1.upper()
        ):
            raise DataSplitError("training and testing token orientation differs")

        training_addresses = self.training.token_addresses
        testing_addresses = self.testing.token_addresses

        if bool(training_addresses) != bool(testing_addresses):
            raise DataSplitError(
                "training and testing contract identity availability differs"
            )

        if (
            training_addresses
            and training_addresses != testing_addresses
        ):
            raise DataSplitError(
                "training and testing token contract addresses differ"
            )

        if not self.training.observations:
            raise DataSplitError("training period cannot be empty")

        if not self.testing.observations:
            raise DataSplitError("testing period cannot be empty")

        if any(
            point.timestamp > self.cutoff_timestamp
            for point in self.training.observations
        ):
            raise DataSplitError("training data contains observations after cutoff")

        if any(
            point.timestamp <= self.cutoff_timestamp
            for point in self.testing.observations
        ):
            raise DataSplitError(
                "testing data contains observations at or before cutoff"
            )

        if (
            self.training.observations[-1].timestamp
            >= self.testing.observations[0].timestamp
        ):
            raise DataSplitError("training and testing periods overlap")


class ChronologicalMarketDataSplitter:
    """Split one pool's price history at an explicit Unix timestamp."""

    def split(
        self,
        market_data: HistoricalMarketData,
        cutoff_timestamp: float,
    ) -> HistoricalMarketDataSplit:
        """Place timestamps <= cutoff in training and later ones in testing.

        The cutoff is inclusive for training and exclusive for testing.
        No observations are shuffled, duplicated, or discarded silently.
        """

        if not isinstance(market_data, HistoricalMarketData):
            raise DataSplitError(
                "market_data must be HistoricalMarketData"
            )

        if (
            isinstance(cutoff_timestamp, bool)
            or not isinstance(cutoff_timestamp, (int, float))
            or not math.isfinite(cutoff_timestamp)
            or cutoff_timestamp < 0
        ):
            raise DataSplitError(
                "cutoff_timestamp must be a non-negative finite number"
            )

        training_points = tuple(
            point
            for point in market_data.observations
            if point.timestamp <= cutoff_timestamp
        )

        testing_points = tuple(
            point
            for point in market_data.observations
            if point.timestamp > cutoff_timestamp
        )

        if not training_points:
            raise DataSplitError(
                "cutoff leaves no observations in the training period"
            )

        if not testing_points:
            raise DataSplitError(
                "cutoff leaves no observations in the testing period"
            )

        training = HistoricalMarketData(
            pool_id=market_data.pool_id,
            token0=market_data.token0,
            token1=market_data.token1,
            chain=market_data.chain,
            observations=training_points,
            token_addresses=market_data.token_addresses,
        )

        testing = HistoricalMarketData(
            pool_id=market_data.pool_id,
            token0=market_data.token0,
            token1=market_data.token1,
            chain=market_data.chain,
            observations=testing_points,
            token_addresses=market_data.token_addresses,
        )

        return HistoricalMarketDataSplit(
            pool_id=market_data.pool_id,
            cutoff_timestamp=float(cutoff_timestamp),
            training=training,
            testing=testing,
        )