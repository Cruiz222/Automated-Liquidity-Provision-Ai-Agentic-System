"""DEX historical backtesting components."""

from .eligibility import (
    EligibilityConfig,
    EligibilityReport,
    EligibilityStatus,
    IdentityConfidence,
    MarketGroup,
    PoolEligibility,
    build_eligibility_report,
    build_eligibility_report_from_database,
)

from .market_data import (
    HistoricalMarketData,
    HistoricalMarketDataLoader,
    PricePoint,
)
from .portfolio import (
    LPPortfolioResult,
    PortfolioModelError,
    PortfolioPoint,
    V2LPPortfolioModel,
)
from .accounting import (
    AccountingModelError,
    ExecutionCostConfig,
    LPAccountingModel,
    NetPortfolioPoint,
    NetPortfolioResult,
)

from .splitting import (
    ChronologicalMarketDataSplitter,
    DataSplitError,
    HistoricalMarketDataSplit,
)

from .oos import (
    DEXOutOfSampleEvaluator,
    OOSBacktestError,
    OOSBacktestResult,
    OOSPoolResult,
)

__all__ = [
    "build_eligibility_report_from_database",
    "build_eligibility_report",
    "PoolEligibility",
    "MarketGroup",
    "IdentityConfidence",
    "EligibilityStatus",
    "EligibilityReport",
    "EligibilityConfig",
    "HistoricalMarketData",
    "HistoricalMarketDataLoader",
    "PricePoint",
    "LPPortfolioResult",
    "PortfolioModelError",
    "PortfolioPoint",
    "V2LPPortfolioModel",
    "AccountingModelError",
    "ExecutionCostConfig",
    "LPAccountingModel",
    "NetPortfolioPoint",
    "NetPortfolioResult",
    "ChronologicalMarketDataSplitter",
    "DataSplitError",
    "HistoricalMarketDataSplit",
    "DEXOutOfSampleEvaluator",
    "OOSBacktestError",
    "OOSBacktestResult",
    "OOSPoolResult",
]
