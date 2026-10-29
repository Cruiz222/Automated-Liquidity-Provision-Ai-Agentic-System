"""Replay a single-instrument CSV with explicit passive execution assumptions."""
import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import platform

from .engine import Config, Engine
from .market_data import parse_csv


@dataclass(frozen=True)
class Execution:
    latency_ms: int = 100
    participation: float = .1
    queue_ahead: float = 0

    def __post_init__(self):
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError('latency_ms must be a nonnegative integer')
        if not math.isfinite(self.participation) or not 0 < self.participation <= 1:
            raise ValueError('participation must be in (0, 1]')
        if not math.isfinite(self.queue_ahead) or self.queue_ahead < 0:
            raise ValueError('queue_ahead must be finite and nonnegative')


class ReplaySession:
    """Incremental execution model shared by CSV replay and live paper observation."""

    def __init__(self, config=None, execution=None, history_limit=None):
        self.config = config or Config()
        self.execution = execution or Execution()
        self.history_limit = history_limit
        self.index = -1
        self.first = None
        self.account = Engine(self.config)
        self.active, self.pending = {}, None
        self.fills, self.decisions, self.curve = [], [], []
        self.submitted_quantity = self.filled_quantity = self.fees = 0.0
        self.inventory_notional_time = self.inventory_time = 0.0
        self.peak = self.config.initial_cash
        self.max_drawdown = self.max_drawdown_pct = self.max_inventory = 0.0
        self.rejected_orders = 0
        self.fill_count = 0
        self.execution_counts = {}
        self.last_execution = {'outcome': 'no_trade'}
        self.previous = None

    def cancel_orders(self):
        self.active = {}
        self.pending = None

    def step(self, event, submit=True, trade=True, decision_time_ms=None):
        if self.previous is not None and event.timestamp_ms < self.previous.timestamp_ms:
            raise ValueError('Events must be chronological')
        if self.first is None:
            self.first = event
        self.index += 1
        if self.previous is not None:
            elapsed = event.timestamp_ms - self.previous.timestamp_ms
            self.inventory_time += self.account.inventory * elapsed
            self.inventory_notional_time += self.account.inventory * self.previous.mid * elapsed
        self.account.price = event.mid
        rejected_sides = set()
        self.last_execution = {'outcome': 'no_trade'}
        if event.trade_side:
            self.last_execution = {'outcome': 'paused' if not trade else 'no_active_quote',
                                   'side': 'bid' if event.trade_side == 'sell' else 'ask'}
        # Replacements are atomic at arrival. Old orders remain live until then.
        if self.pending is not None and event.timestamp_ms >= self.pending['arrival_ms']:
            self.active = {}
            for side, order in self.pending['orders'].items():
                marketable = order['price'] >= event.ask if side == 'bid' else order['price'] <= event.bid
                if marketable:
                    self.rejected_orders += 1
                    rejected_sides.add(side)
                else:
                    self.active[side] = dict(order)
            self.pending = None
        if trade and event.trade_side:
            side = 'bid' if event.trade_side == 'sell' else 'ask'
            order = self.active.get(side)
            if order is None:
                if side in rejected_sides:
                    self.last_execution['outcome'] = 'post_only_rejected'
                elif self.pending and side in self.pending['orders'] and self.pending['arrival_ms'] > event.timestamp_ms:
                    self.last_execution['outcome'] = 'waiting_latency'
            else:
                reached = order['price'] >= event.trade_price if side == 'bid' else order['price'] <= event.trade_price
                self.last_execution.update(outcome='price_not_reached' if not reached else 'order_exhausted',
                                           limit_price=order['price'],
                                           distance_bps=max(0, (event.trade_price - order['price'] if side == 'bid' else order['price'] - event.trade_price) / order['price'] * 10_000))
            if order and (order['price'] >= event.trade_price if side == 'bid' else order['price'] <= event.trade_price):
                volume = event.trade_size * self.execution.participation
                queue_consumed = min(order['queue'], volume)
                order['queue'] -= queue_consumed
                volume -= queue_consumed
                if side == 'bid':
                    capacity = min(self.config.max_inventory - self.account.inventory,
                                   self.account.cash / (order['price'] * (1 + self.config.fee_bps / 10_000)))
                else:
                    capacity = self.account.inventory
                size = max(0.0, min(order['remaining'], volume, capacity))
                if order['remaining'] <= 1e-12:
                    self.last_execution['outcome'] = 'order_exhausted'
                elif volume <= 1e-12:
                    self.last_execution['outcome'] = 'queue_ahead' if queue_consumed else 'negligible_volume'
                elif capacity <= 1e-12:
                    self.last_execution['outcome'] = 'account_limit'
                if size > 1e-12:
                    self.last_execution.update(outcome='filled', filled_size=size)
                    fee = order['price'] * size * self.config.fee_bps / 10_000
                    self.account.cash += (-order['price'] * size if side == 'bid' else order['price'] * size) - fee
                    self.account.inventory += size if side == 'bid' else -size
                    order['remaining'] -= size
                    self.filled_quantity += size
                    self.fees += fee
                    self.fill_count += 1
                    self.fills.append({'event': self.index, 'timestamp_ms': event.timestamp_ms,
                                  'submitted_event': order['submitted_event'],
                                  'side': 'buy' if side == 'bid' else 'sell',
                                  'price': order['price'], 'size': size, 'fee': fee})
        if event.trade_side:
            outcome = self.last_execution['outcome']
            self.execution_counts[outcome] = self.execution_counts.get(outcome, 0) + 1
        equity = self.account.cash + self.account.inventory * event.mid
        self.peak = max(self.peak, equity)
        self.max_drawdown = max(self.max_drawdown, self.peak - equity)
        self.max_drawdown_pct = max(self.max_drawdown_pct, (self.peak - equity) / self.peak * 100)
        self.max_inventory = max(self.max_inventory, self.account.inventory)
        self.curve.append({'timestamp_ms': event.timestamp_ms, 'equity': equity,
                      'cash': self.account.cash, 'inventory': self.account.inventory, 'mid': event.mid})
        # Keep a single in-flight replacement, rather than cancelling before arrival.
        if self.pending is None and submit:
            quotes = self.account.quotes()
            orders = {side: {'price': price, 'remaining': self.config.order_size,
                             'queue': self.execution.queue_ahead, 'submitted_event': self.index}
                      for side, price in quotes.items() if price is not None}
            self.submitted_quantity += sum(order['remaining'] for order in orders.values())
            decision_time = event.timestamp_ms if decision_time_ms is None else max(event.timestamp_ms, decision_time_ms)
            self.pending = {'arrival_ms': decision_time + self.execution.latency_ms, 'orders': orders}
            self.decisions.append({'event': self.index, 'timestamp_ms': event.timestamp_ms,
                              'arrival_ms': self.pending['arrival_ms'], 'quotes': quotes})
        self.previous = event

        if self.history_limit is not None:
            self.fills = self.fills[-self.history_limit:]
            self.decisions = self.decisions[-self.history_limit:]
            self.curve = self.curve[-self.history_limit:]

    def report(self):
        if self.previous is None:
            raise ValueError('No market events processed')
        duration = self.previous.timestamp_ms - self.first.timestamp_ms
        final = self.curve[-1]
        return {
            'metrics': {
                'net_pnl': final['equity'] - self.config.initial_cash,
                'return_pct': (final['equity'] / self.config.initial_cash - 1) * 100,
                'final_equity': final['equity'], 'final_cash': self.account.cash,
                'final_inventory': self.account.inventory, 'fees': self.fees,
                'max_drawdown': self.max_drawdown, 'max_drawdown_pct': self.max_drawdown_pct,
                'max_inventory': self.max_inventory,
                'time_weighted_inventory': self.inventory_time / duration if duration else None,
                'time_weighted_inventory_notional': self.inventory_notional_time / duration if duration else None,
                'fill_count': self.fill_count, 'filled_quantity': self.filled_quantity,
                'submitted_quantity': self.submitted_quantity,
                'quantity_fill_rate': self.filled_quantity / self.submitted_quantity if self.submitted_quantity else 0,
                'post_only_rejections': self.rejected_orders,
                'liquidation_value_at_final_bid_after_fee': self.account.cash + self.account.inventory * self.previous.bid * (1 - self.config.fee_bps / 10_000),
            },
            'execution_diagnostics': dict(self.execution_counts),
            'fills': self.fills, 'decisions': self.decisions, 'equity_curve': self.curve,
        }


def run(events, config=None, execution=None):
    """Input order breaks timestamp ties; current-row data cannot generate its own fill."""
    if len(events) < 2:
        raise ValueError('At least two market events are required')
    session = ReplaySession(config, execution)
    for index, event in enumerate(events):
        session.step(event, submit=index < len(events) - 1)
    return session.report()


def compare(events, config=None, execution=None):
    config, execution = config or Config(), execution or Execution()
    agent = run(events, config, execution)
    baseline = run(events, replace(config, inventory_skew_bps=0), execution)
    return {
        'report_version': 1, 'model_version': 'passive-trade-replay-v1',
        'python_version': platform.python_version(),
        'market': {'event_count': len(events), 'start_ms': events[0].timestamp_ms,
                   'end_ms': events[-1].timestamp_ms},
        'config': asdict(config), 'execution': asdict(execution),
        'assumptions': [
            'One instrument; cash and prices use the same quote currency; all sizes use base units.',
            'Each row is a book snapshot immediately before its optional aggressor trade.',
            'Rows with equal timestamps retain CSV order; new quotes execute only on later rows.',
            'Old quotes remain active until a replacement arrives; marketable replacements are rejected per side.',
            'Only compatible trades can fill; volume is trade size times participation, less assumed queue ahead.',
            'Queue ahead is reset per replacement; top-of-book sizes do not reveal queue depth at our price.',
            'Fills execute at our limit; no book-crossing, hidden-liquidity, market-impact or exchange-specific model.',
            'Inventory is long-only; stale orders are clipped to current cash and inventory limits.',
            'Equity uses mid prices; final bid liquidation is an estimate without depth or slippage.',
            'Fill rate is filled base quantity divided by submitted base quantity, including replacements.',
            'Time-weighted exposure holds the previous event inventory and mid until the next event.',
            'Baseline uses the same spread, execution and limits with zero inventory skew; cash benchmark P&L is zero.',
        ],
        'agent': agent, 'baseline': baseline,
        'comparison': {'net_pnl_difference': agent['metrics']['net_pnl'] - baseline['metrics']['net_pnl'],
                       'cash_benchmark_net_pnl': 0},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--latency-ms', type=int, default=100)
    parser.add_argument('--participation', type=float, default=.1)
    parser.add_argument('--queue-ahead', type=float, default=0)
    parser.add_argument('--fee-bps', type=float, default=2)
    parser.add_argument('--order-size', type=float, default=1)
    parser.add_argument('--max-inventory', type=float, default=10)
    parser.add_argument('--half-spread-bps', type=float, default=12)
    parser.add_argument('--inventory-skew-bps', type=float, default=20)
    args = parser.parse_args()
    try:
        source_bytes = Path(args.csv).read_bytes()
        events = parse_csv(source_bytes.decode('utf-8-sig'))
        config = Config(initial_price=events[0].mid, fee_bps=args.fee_bps,
                        order_size=args.order_size, max_inventory=args.max_inventory,
                        half_spread_bps=args.half_spread_bps, inventory_skew_bps=args.inventory_skew_bps)
        report = compare(events, config, Execution(args.latency_ms, args.participation, args.queue_ahead))
        report['source'] = {'file': str(args.csv), 'sha256': hashlib.sha256(source_bytes).hexdigest()}
        output = json.dumps(report, indent=2, allow_nan=False) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # Refuse to overwrite an input, a journal, or an earlier research result.
            with args.output.open('x', encoding='utf-8') as destination:
                destination.write(output)
            print(json.dumps({'output': str(args.output), 'agent': report['agent']['metrics'],
                              'baseline': report['baseline']['metrics']}, indent=2))
        else:
            print(output, end='')
    except (OSError, ValueError, OverflowError) as error:
        parser.exit(1, f'Backtest failed: {error}\n')


if __name__ == '__main__':
    main()
