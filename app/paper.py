"""Read-only OKX feed capture and local paper execution."""
import argparse
import copy
from dataclasses import asdict, dataclass, field, replace
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time
from urllib.error import URLError
import uuid

from .backtest import Execution, ReplaySession
from .engine import Config
from .market_data import MarketEvent
from .okx_public import BASE_URLS, DEFAULT_BASE_URL, FeedError, OKXPublic, normalize
from .paper_store import PaperStore, export_csv


@dataclass
class FeedState:
    book: MarketEvent | None = None
    books: list[MarketEvent] = field(default_factory=list)
    segment_start_ms: int | None = None
    warmup_skipped_trades: int = 0
    anchor: int | None = None
    received_ms: int | None = None
    status: str = 'waiting'
    error: str | None = None
    segment: int = 0
    gaps: int = 0
    duplicates: int = 0
    skipped_trades: int = 0


class PaperRunner:
    def __init__(self, store, instrument='BTC-USDT', config=None, execution=None,
                 stale_ms=5000, clock=lambda: int(time.time() * 1000)):
        if stale_ms <= 0:
            raise ValueError('stale_ms must be positive')
        self.store, self.instrument, self.stale_ms, self.clock = store, instrument, stale_ms, clock
        self.session = ReplaySession(config or Config(order_size=.001, max_inventory=.01),
                                     execution, history_limit=200)
        self.feed = FeedState()
        self.running = True
        self.storage_error = None
        self.lock = threading.RLock()

    def _append(self, batch):
        try:
            self.store.append(batch)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.session.cancel_orders()
            self.storage_error = f'Paper journal failed; start a new run: {error}'
            raise RuntimeError(self.storage_error) from error

    def fail(self, error, raw=None):
        with self.lock:
            self.session.cancel_orders()
            self.feed.book = None
            self.feed.books = []
            self.feed.anchor = None
            self.feed.status = 'disconnected'
            self.feed.error = str(error)
            self.feed.gaps += 1
            if not self.storage_error:
                self._append({'received_ms': self.clock(), 'raw': raw, 'events': [],
                              'health': self.health(), 'control': 'feed_fault'})

    def health(self):
        age = None if self.feed.received_ms is None else max(0, self.clock() - self.feed.received_ms)
        status = self.feed.status
        if age is not None and age > self.stale_ms:
            status = 'stale'
        if self.storage_error:
            status = 'storage_failed'
        return {'status': status, 'error': self.feed.error, 'age_ms': age,
                'gaps': self.feed.gaps, 'duplicates': self.feed.duplicates,
                'skipped_trades': self.feed.skipped_trades, 'segment': self.feed.segment,
                'warmup_skipped_trades': self.feed.warmup_skipped_trades,
                'buffered_books': sum(self.session.previous is None or b.timestamp_ms > self.session.previous.timestamp_ms for b in self.feed.books),
                'alignment_lag_ms': max(0, self.feed.book.timestamp_ms - self.session.previous.timestamp_ms) if self.feed.book and self.session.previous else None,
                'instrument': self.instrument, 'transport': 'OKX public REST',
                'continuity': 'overlap checked; completeness not guaranteed'}

    def ingest(self, raw):
        with self.lock:
            if self.storage_error:
                raise RuntimeError(self.storage_error)
            try:
                return self._ingest(raw)
            except FeedError as error:
                self.fail(error, raw)
                return False

    def _ingest(self, raw):
        received = self.clock()
        book, trades, duplicates = normalize(raw, self.instrument)
        if received - book.timestamp_ms > self.stale_ms or book.timestamp_ms > received + 1000:
            raise FeedError('Stale book or exchange clock is ahead of local clock')
        if trades and trades[-1][1].timestamp_ms > received + 1000:
            raise FeedError('Trade timestamp is ahead of local clock')
        feed = copy.copy(self.feed)
        feed.books = list(self.feed.books)
        session = copy.deepcopy(self.session, {id(self.session.account.lock): threading.RLock()})
        feed.duplicates += duplicates
        discontinuity = None
        if feed.received_ms is not None and received - feed.received_ms > self.stale_ms:
            discontinuity = 'Polling interval exceeded freshness limit'
        if feed.book is not None and book.timestamp_ms < feed.book.timestamp_ms:
            raise FeedError('Book timestamp regressed')
        if feed.anchor is not None and feed.anchor not in {identifier for identifier, _ in trades}:
            discontinuity = 'Previous trade anchor is missing from the recent-trades window'
        new_trades = [(identifier, event) for identifier, event in trades
                      if feed.anchor is None or identifier > feed.anchor]
        feed.duplicates += sum(identifier <= feed.anchor for identifier, _ in trades) if feed.anchor is not None else 0
        previous_time = session.previous.timestamp_ms if session.previous else -1
        warmup_skipped = 0
        if feed.book is not None:
            if not feed.books or book.timestamp_ms > feed.books[-1].timestamp_ms:
                feed.books.append(book)
            eligible = []
            for identifier, event in new_trades:
                if feed.segment_start_ms is not None and event.timestamp_ms < feed.segment_start_ms:
                    warmup_skipped += 1
                    continue
                if event.timestamp_ms < previous_time:
                    discontinuity = 'Late trade would move the paper clock backwards'
                candidates = [b for b in feed.books if b.timestamp_ms <= event.timestamp_ms]
                if not candidates or event.timestamp_ms - candidates[-1].timestamp_ms > self.stale_ms:
                    discontinuity = 'No fresh earlier book is available for a new trade'
                eligible.append((identifier, event))
            new_trades = eligible
        if discontinuity:
            session.cancel_orders()
            feed.gaps += 1
            feed.book = None
        records = []
        def process(event, submit=True):
            session.step(event, submit=self.running and submit, trade=self.running, decision_time_ms=received)
            records.append({
                'segment': feed.segment, 'market': asdict(event),
                'execution': dict(session.last_execution),
                'decision': session.decisions[-1] if session.decisions and session.decisions[-1]['event'] == session.index else None,
                'fills': [fill for fill in session.fills if fill['event'] == session.index],
                'account': {'cash': session.account.cash, 'inventory': session.account.inventory,
                            'equity': session.curve[-1]['equity'], 'fees': session.fees},
                'active_orders': copy.deepcopy(session.active), 'pending_orders': copy.deepcopy(session.pending),
                'running': self.running,
            })
        if feed.book is None:
            # Bootstrap never executes the historical trades returned by REST.
            if book.timestamp_ms < previous_time:
                raise FeedError('Cannot resynchronize before the last accepted event')
            session.cancel_orders()
            feed.segment += 1
            feed.segment_start_ms = book.timestamp_ms
            feed.books = [book]
            feed.skipped_trades += len(trades)
            process(book)
            feed.status = 'synchronizing'
        else:
            feed.warmup_skipped_trades += warmup_skipped
            feed.skipped_trades += warmup_skipped
            # The newest observed trade is the event-time watermark. Book
            # snapshots ahead of it stay buffered, never advancing execution.
            watermark = new_trades[-1][1].timestamp_ms if new_trades else previous_time
            timeline = [(b.timestamp_ms, 0, 0, b) for b in feed.books
                        if previous_time < b.timestamp_ms <= watermark]
            timeline.extend((event.timestamp_ms, 1, identifier, event) for identifier, event in new_trades)
            timeline.sort(key=lambda item: item[:3])
            for _, kind, _, event in timeline:
                if kind == 1:
                    aligned_book = next(b for b in reversed(feed.books) if b.timestamp_ms <= event.timestamp_ms)
                    event = replace(aligned_book, timestamp_ms=event.timestamp_ms,
                                    trade_price=event.trade_price, trade_size=event.trade_size,
                                    trade_side=event.trade_side)
                process(event)
            feed.status = 'healthy'
            # Retain one predecessor for alignment and a bounded window of
            # newer snapshots. Lost older coverage still causes a real gap.
            cutoff = max(session.previous.timestamp_ms, book.timestamp_ms - self.stale_ms)
            predecessors = [b for b in feed.books if b.timestamp_ms <= cutoff]
            feed.books = (predecessors[-1:] + [b for b in feed.books if b.timestamp_ms > cutoff])[-512:]
        feed.book = book
        feed.anchor = trades[-1][0] if trades else None
        feed.received_ms = received
        feed.error = discontinuity
        self._append({'received_ms': received, 'raw': raw, 'events': records,
                      'health': asdict(feed), 'control': None})
        self.feed, self.session = feed, session
        return True

    def set_running(self, running):
        with self.lock:
            if self.storage_error:
                raise RuntimeError(self.storage_error)
            if self.running == running:
                return
            self._append({'received_ms': self.clock(), 'raw': None, 'events': [],
                          'health': self.health(), 'control': 'resume' if running else 'pause'})
            self.running = running
            self.session.cancel_orders()
            # Resume starts from a fresh snapshot and ignores catch-up trades.
            if running:
                self.feed.book = None
                self.feed.books = []
                self.feed.anchor = None
                self.feed.status = 'synchronizing'

    def snapshot(self):
        with self.lock:
            account = self.session.account
            health = self.health()
            available = self.running and health['status'] == 'healthy'
            equity = account.cash + account.inventory * account.price
            quotes = {side: self.session.active[side]['price'] if available and side in self.session.active
                      and self.session.active[side]['remaining'] > 1e-12 else None for side in ('bid', 'ask')}
            return {'mode': 'paper', 'instrument': self.instrument, 'running': self.running,
                    'tick': self.session.index + 1, 'price': account.price,
                    'cash': account.cash, 'inventory': account.inventory, 'equity': equity,
                    'pnl': equity - account.config.initial_cash, 'fees': self.session.fees,
                    'quotes': quotes, 'config': asdict(account.config),
                    'execution': asdict(self.session.execution), 'feed': health,
                    'storage_error': self.storage_error,
                    'trades': [dict(fill, tick=fill['event'] + 1) for fill in reversed(self.session.fills)],
                    'history': [dict(tick=max(1, self.session.index + 2 - len(self.session.curve)) + i,
                                     price=row['mid'], equity=row['equity']) for i, row in enumerate(self.session.curve)],
                    'decisions': self.session.decisions[-10:],
                    'execution_diagnostics': dict(self.session.execution_counts),
                    'metrics': self.session.report()['metrics'] if self.session.previous else {},
                    'pending_orders': copy.deepcopy(self.session.pending) if available else None}

    def close(self):
        with self.lock:
            self.session.cancel_orders()
            self.store.close()


def poll_once(runner, client):
    try:
        return runner.ingest(client.poll())
    except (FeedError, URLError, OSError, ValueError, KeyError, TypeError) as error:
        runner.fail(error)
        return False


def collect(runner, client, polls=0, duration_seconds=None, interval=1,
            clock=time.monotonic, sleep=time.sleep, emit=print):
    if duration_seconds is not None and (not math.isfinite(duration_seconds) or duration_seconds <= 0):
        raise ValueError('duration_seconds must be finite and positive')
    started = clock()
    deadline = started + duration_seconds if duration_seconds is not None else None
    count = successes = 0
    while (polls == 0 or count < polls) and (deadline is None or clock() < deadline):
        accepted = poll_once(runner, client)
        count += 1
        successes += int(accepted)
        emit(json.dumps({'poll': count, 'accepted': accepted, 'feed': runner.snapshot()['feed']}))
        if polls and count >= polls:
            break
        delay = min(30, interval * (2 if not accepted else 1))
        if deadline is not None:
            delay = min(delay, max(0, deadline - clock()))
        if delay:
            sleep(delay)
    return {'polls': count, 'successful_polls': successes, 'elapsed_seconds': clock() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    run = subparsers.add_parser('run', help='Capture public data and run simulated orders')
    run.add_argument('--run-dir', type=Path, default=Path('data') / ('paper-' + uuid.uuid4().hex[:10]))
    run.add_argument('--instrument', default='BTC-USDT')
    run.add_argument('--base-url', choices=BASE_URLS, default=DEFAULT_BASE_URL)
    run.add_argument('--dns-mode', choices=('system', 'doh'), default='system')
    run.add_argument('--polls', type=int, default=0, help='0 means continuous; positive values bound the run')
    run.add_argument('--duration-seconds', type=float, help='Stop after this elapsed duration; an in-flight request may finish later')
    run.add_argument('--interval', type=float, default=1)
    run.add_argument('--stale-ms', type=int, default=5000)
    run.add_argument('--latency-ms', type=int, default=100)
    run.add_argument('--participation', type=float, default=.1)
    run.add_argument('--queue-ahead', type=float, default=0)
    run.add_argument('--fee-bps', type=float, default=2)
    run.add_argument('--order-size', type=float, default=.001)
    run.add_argument('--max-inventory', type=float, default=.01)
    run.add_argument('--port', type=int, default=8000)
    run.add_argument('--no-dashboard', action='store_true')
    run.add_argument('--registry', type=Path, default=Path('data/experiments.sqlite3'))
    export = subparsers.add_parser('export', help='Export separate validated replay CSV segments')
    export.add_argument('journal', type=Path)
    export.add_argument('output_dir', type=Path)
    args = parser.parse_args()
    if args.command == 'export':
        print(json.dumps(export_csv(args.journal, args.output_dir), indent=2))
        return
    if not .5 <= args.interval <= 60 or args.polls < 0 or args.stale_ms <= 0:
        parser.error('interval must be between 0.5 and 60 seconds, polls nonnegative, and stale-ms positive')
    if args.duration_seconds is not None and (not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0):
        parser.error('duration-seconds must be finite and positive')
    client = OKXPublic(args.instrument, base_url=args.base_url, dns_mode=args.dns_mode)
    config = Config(order_size=args.order_size, max_inventory=args.max_inventory, fee_bps=args.fee_bps)
    execution = Execution(args.latency_ms, args.participation, args.queue_ahead)
    try:
        store = PaperStore(args.run_dir, {'version': 1, 'instrument': args.instrument,
                                        'config': asdict(config), 'execution': asdict(execution),
                                        'stale_ms': args.stale_ms, 'source': 'OKX public REST', 'base_url': args.base_url, 'dns_mode': args.dns_mode,
                                        'model': 'passive-trade-replay-v1; held prior REST book approximation',
                                    'alignment_model': 'trade-watermark-v1',
                                        'collection': {'polls': args.polls, 'duration_seconds': args.duration_seconds, 'interval_seconds': args.interval}})
    except FileExistsError:
        parser.error(f'Run directory already exists: {args.run_dir}. Omit --run-dir to generate a fresh directory, or choose a new path.')
    runner = PaperRunner(store, args.instrument, config, execution, args.stale_ms)
    http = worker = None
    successful_polls = 0
    try:
        if not args.no_dashboard:
            from http.server import ThreadingHTTPServer
            from . import server
            server.engine = runner
            server.registry_path = args.registry
            http = ThreadingHTTPServer(('127.0.0.1', args.port), server.Handler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            print(f'Paper dashboard: http://127.0.0.1:{args.port}', flush=True)
        print(f'Public data / simulated orders. Journal: {args.run_dir / "paper.sqlite3"}', flush=True)
        collection = collect(runner, client, args.polls, args.duration_seconds, args.interval)
        successful_polls = collection['successful_polls']
    except KeyboardInterrupt:
        successful_polls = int(runner.feed.received_ms is not None)
    finally:
        if http:
            http.shutdown()
            http.server_close()
            worker.join()
        runner.close()
    from .capture_review import review
    report = review(args.run_dir / 'paper.sqlite3')
    with (args.run_dir / 'capture-review.json').open('x') as target:
        json.dump(report, target, indent=2, allow_nan=False)
    print(f'Capture review: {report["status"]}; {args.run_dir / "capture-review.json"}', flush=True)
    if (args.polls or args.duration_seconds is not None) and not successful_polls:
        parser.exit(2, 'No fresh market data was accepted; inspect the journal and network connectivity.\n')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
