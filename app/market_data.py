"""Strict CSV import for one instrument, with prices and sizes in consistent units."""
import csv
import io
from dataclasses import dataclass
import math
from pathlib import Path


COLUMNS = ('timestamp_ms', 'bid', 'ask', 'bid_size', 'ask_size',
           'trade_price', 'trade_size', 'trade_side')


@dataclass(frozen=True)
class MarketEvent:
    timestamp_ms: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    trade_price: float | None = None
    trade_size: float | None = None
    trade_side: str | None = None

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def __post_init__(self):
        if type(self.timestamp_ms) is not int or self.timestamp_ms < 0:
            raise ValueError('timestamp_ms must be a nonnegative integer')
        for name in ('bid', 'ask', 'bid_size', 'ask_size'):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or (name in ('bid', 'ask') and value == 0):
                raise ValueError(f'{name} must be finite and {"positive" if name in ("bid", "ask") else "nonnegative"}')
        if self.bid >= self.ask:
            raise ValueError('bid must be below ask')
        trade = (self.trade_price, self.trade_size, self.trade_side)
        if any(value is not None for value in trade):
            if any(value is None for value in trade):
                raise ValueError('trade_price, trade_size and trade_side must all be supplied together')
            if not all(math.isfinite(value) and value > 0 for value in trade[:2]):
                raise ValueError('trade price and size must be finite and positive')
            if self.trade_side not in ('buy', 'sell'):
                raise ValueError('trade_side must be the aggressor side: buy or sell')


def parse_csv(text):
    events = []
    with io.StringIO(text, newline='') as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(COLUMNS):
            raise ValueError('CSV header must be: ' + ','.join(COLUMNS))
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('wrong number of columns')
                row = {key: value.strip() for key, value in row.items()}
                event = MarketEvent(
                    timestamp_ms=int(row['timestamp_ms']),
                    **{key: float(row[key]) for key in ('bid', 'ask', 'bid_size', 'ask_size')},
                    trade_price=float(row['trade_price']) if row['trade_price'] else None,
                    trade_size=float(row['trade_size']) if row['trade_size'] else None,
                    trade_side=row['trade_side'] or None,
                )
                if events and event.timestamp_ms < events[-1].timestamp_ms:
                    raise ValueError('timestamps must be nondecreasing; rows are never sorted automatically')
                events.append(event)
            except (ValueError, TypeError, OverflowError) as error:
                raise ValueError(f'CSV line {reader.line_num}: {error}') from error
    if len(events) < 2:
        raise ValueError('At least two market events are required')
    return events


def load_csv(path):
    return parse_csv(Path(path).read_text(encoding='utf-8-sig'))
