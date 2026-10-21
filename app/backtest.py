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


def run(events, config=None, execution=None):
    """Input order breaks timestamp ties; current-row data cannot generate its own fill."""
    if len(events) < 2:
        raise ValueError('At least two market events are required')
    if any(b.timestamp_ms < a.timestamp_ms for a, b in zip(events, events[1:])):
        raise ValueError('Events must be chronological')
    config, execution = config or Config(), execution or Execution()
    account = Engine(config)
    active, pending = {}, None
    fills, decisions, curve = [], [], []
    submitted_quantity = filled_quantity = fees = 0.0
    inventory_notional_time = inventory_time = 0.0
    peak = config.initial_cash
    max_drawdown = max_drawdown_pct = max_inventory = 0.0
    rejected_orders = 0
    previous = None
    for index, event in enumerate(events):
        if previous is not None:
            elapsed = event.timestamp_ms - previous.timestamp_ms
            inventory_time += account.inventory * elapsed
            inventory_notional_time += account.inventory * previous.mid * elapsed
        account.price = event.mid
        # Replacements are atomic at arrival. Old orders remain live until then.
        if pending is not None and event.timestamp_ms >= pending['arrival_ms']:
            active = {}
            for side, order in pending['orders'].items():
                marketable = order['price'] >= event.ask if side == 'bid' else order['price'] <= event.bid
                if marketable:
                    rejected_orders += 1
                else:
                    active[side] = dict(order)
            pending = None
        if event.trade_side:
            side = 'bid' if event.trade_side == 'sell' else 'ask'
            order = active.get(side)
            if order and (order['price'] >= event.trade_price if side == 'bid' else order['price'] <= event.trade_price):
                volume = event.trade_size * execution.participation
                queue_consumed = min(order['queue'], volume)
                order['queue'] -= queue_consumed
                volume -= queue_consumed
                if side == 'bid':
                    capacity = min(config.max_inventory - account.inventory,
                                   account.cash / (order['price'] * (1 + config.fee_bps / 10_000)))
                else:
                    capacity = account.inventory
                size = max(0.0, min(order['remaining'], volume, capacity))
                if size > 1e-12:
                    fee = order['price'] * size * config.fee_bps / 10_000
                    account.cash += (-order['price'] * size if side == 'bid' else order['price'] * size) - fee
                    account.inventory += size if side == 'bid' else -size
                    order['remaining'] -= size
                    filled_quantity += size
                    fees += fee
                    fills.append({'event': index, 'timestamp_ms': event.timestamp_ms,
                                  'submitted_event': order['submitted_event'],
                                  'side': 'buy' if side == 'bid' else 'sell',
                                  'price': order['price'], 'size': size, 'fee': fee})
        equity = account.cash + account.inventory * event.mid
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        max_drawdown_pct = max(max_drawdown_pct, (peak - equity) / peak * 100)
        max_inventory = max(max_inventory, account.inventory)
        curve.append({'timestamp_ms': event.timestamp_ms, 'equity': equity,
                      'cash': account.cash, 'inventory': account.inventory, 'mid': event.mid})
        # Keep a single in-flight replacement, rather than cancelling before arrival.
        # No final-row submission: there is no remaining event that could execute it.
        if pending is None and index < len(events) - 1:
            quotes = account.quotes()
            orders = {side: {'price': price, 'remaining': config.order_size,
                             'queue': execution.queue_ahead, 'submitted_event': index}
                      for side, price in quotes.items() if price is not None}
            submitted_quantity += sum(order['remaining'] for order in orders.values())
            pending = {'arrival_ms': event.timestamp_ms + execution.latency_ms, 'orders': orders}
            decisions.append({'event': index, 'timestamp_ms': event.timestamp_ms,
                              'arrival_ms': pending['arrival_ms'], 'quotes': quotes})
        previous = event
    duration = events[-1].timestamp_ms - events[0].timestamp_ms
    final = curve[-1]
    return {
        'metrics': {
            'net_pnl': final['equity'] - config.initial_cash,
            'return_pct': (final['equity'] / config.initial_cash - 1) * 100,
            'final_equity': final['equity'], 'final_cash': account.cash,
            'final_inventory': account.inventory, 'fees': fees,
            'max_drawdown': max_drawdown, 'max_drawdown_pct': max_drawdown_pct,
            'max_inventory': max_inventory,
            'time_weighted_inventory': inventory_time / duration if duration else None,
            'time_weighted_inventory_notional': inventory_notional_time / duration if duration else None,
            'fill_count': len(fills), 'filled_quantity': filled_quantity,
            'submitted_quantity': submitted_quantity,
            'quantity_fill_rate': filled_quantity / submitted_quantity if submitted_quantity else 0,
            'post_only_rejections': rejected_orders,
            'liquidation_value_at_final_bid_after_fee': account.cash + account.inventory * events[-1].bid * (1 - config.fee_bps / 10_000),
        },
        'fills': fills, 'decisions': decisions, 'equity_curve': curve,
    }


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
