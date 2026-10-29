"""Deterministic, simulated liquidity provision. No exchange connectivity."""
from dataclasses import asdict, dataclass
import random
import threading


@dataclass(frozen=True)
class Config:
    initial_cash: float = 100_000
    initial_price: float = 100
    order_size: float = 1
    max_inventory: float = 10
    half_spread_bps: float = 12
    inventory_skew_bps: float = 20
    fee_bps: float = 2

    def __post_init__(self):
        import math
        if not all(math.isfinite(value) for value in asdict(self).values()):
            raise ValueError('Configuration values must be finite')
        if min(self.initial_cash, self.initial_price, self.order_size, self.max_inventory) <= 0:
            raise ValueError('Cash, price, size and inventory limit must be positive')
        if min(self.half_spread_bps, self.inventory_skew_bps, self.fee_bps) < 0:
            raise ValueError('Spread, skew and fees cannot be negative')


class Engine:
    def __init__(self, config=None, seed=7):
        self.config = config or Config()
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.cash = self.config.initial_cash
        self.inventory = 0.0
        self.price = self.config.initial_price
        self.tick = 0
        self.running = True
        self.fees = 0.0
        self.trades = []
        self.history = []

    def quotes(self):
        c = self.config
        skew = self.inventory / c.max_inventory * c.inventory_skew_bps / 10_000
        center = self.price * (1 - skew)
        bid = max(0.01, min(self.price - .01, center - self.price * c.half_spread_bps / 10_000))
        ask = max(self.price + .01, center + self.price * c.half_spread_bps / 10_000)
        return {
            'bid': bid if self.inventory + c.order_size <= c.max_inventory and self.cash >= bid * c.order_size * (1 + c.fee_bps / 10_000) else None,
            'ask': ask if self.inventory >= c.order_size else None,
        }

    def step(self):
        with self.lock:
            if not self.running:
                return
            quotes = self.quotes()
            self.tick += 1
            self.price = max(1, self.price * (1 + self.rng.gauss(0, .0015)))
            # A quote fills only when the next simulated market price crosses it.
            for side, quote in quotes.items():
                if quote is None or not (self.price <= quote if side == 'bid' else self.price >= quote):
                    continue
                size = self.config.order_size
                fee = quote * size * self.config.fee_bps / 10_000
                self.cash += (-quote * size if side == 'bid' else quote * size) - fee
                self.inventory += size if side == 'bid' else -size
                self.fees += fee
                self.trades.append({'tick': self.tick, 'side': 'buy' if side == 'bid' else 'sell', 'price': quote, 'size': size, 'fee': fee})
            self.trades = self.trades[-100:]
            self.history.append({'tick': self.tick, 'price': self.price, 'equity': self.cash + self.inventory * self.price})
            self.history = self.history[-200:]

    def snapshot(self):
        with self.lock:
            equity = self.cash + self.inventory * self.price
            return {'mode': 'simulation', 'running': self.running, 'tick': self.tick,
                    'price': self.price, 'cash': self.cash, 'inventory': self.inventory,
                    'equity': equity, 'pnl': equity - self.config.initial_cash,
                    'fees': self.fees, 'quotes': self.quotes(), 'config': asdict(self.config),
                    'trades': list(reversed(self.trades)), 'history': list(self.history)}

    def set_running(self, running):
        with self.lock:
            self.running = running
