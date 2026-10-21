"""Read-only OKX feed capture and local paper execution."""
import argparse
import copy
from dataclasses import asdict, dataclass, replace
import json
import logging
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
                      if feed.anchor is not None and identifier > feed.anchor]
        feed.duplicates += sum(identifier <= feed.anchor for identifier, _ in trades) if feed.anchor is not None else 0
        previous_time = session.previous.timestamp_ms if session.previous else -1
        if new_trades and new_trades[0][1].timestamp_ms < previous_time:
            discontinuity = 'Late trade would move the paper clock backwards'
        for _, event in new_trades:
            aligned_book = book if event.timestamp_ms >= book.timestamp_ms else feed.book
            if aligned_book is None or not 0 <= event.timestamp_ms - aligned_book.timestamp_ms <= self.stale_ms:
                discontinuity = 'No fresh earlier book is available for a new trade'
        if discontinuity:
            session.cancel_orders()
            feed.gaps += 1
            feed.book = None
        records = []
        def process(event, submit=True):
            session.step(event, submit=self.running and submit, trade=self.running, decision_time_ms=received)
            records.append({
                'segment': feed.segment, 'market': asdict(event),
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
            feed.skipped_trades += len(trades)
            process(book)
            feed.status = 'synchronizing'
        else:
            inserted_book = False
            for _, event in new_trades:
                if not inserted_book and book.timestamp_ms <= event.timestamp_ms:
                    if book.timestamp_ms > feed.book.timestamp_ms and book.timestamp_ms >= session.previous.timestamp_ms:
                        process(book)
                    inserted_book = True
                aligned_book = book if inserted_book else feed.book
                aligned = replace(aligned_book, timestamp_ms=event.timestamp_ms,
                                  trade_price=event.trade_price, trade_size=event.trade_size,
                                  trade_side=event.trade_side)
                process(aligned)
            if not inserted_book and book.timestamp_ms > feed.book.timestamp_ms and book.timestamp_ms >= session.previous.timestamp_ms:
                process(book)
            feed.status = 'healthy'
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    run = subparsers.add_parser('run', help='Capture public data and run simulated orders')
    run.add_argument('--run-dir', type=Path, default=Path('data') / ('paper-' + uuid.uuid4().hex[:10]))
    run.add_argument('--instrument', default='BTC-USDT')
    run.add_argument('--base-url', choices=BASE_URLS, default=DEFAULT_BASE_URL)
    run.add_argument('--polls', type=int, default=0, help='0 means continuous; positive values bound the run')
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
    export = subparsers.add_parser('export', help='Export separate validated replay CSV segments')
    export.add_argument('journal', type=Path)
    export.add_argument('output_dir', type=Path)
    args = parser.parse_args()
    if args.command == 'export':
        print(json.dumps(export_csv(args.journal, args.output_dir), indent=2))
        return
    if not .5 <= args.interval <= 60 or args.polls < 0 or args.stale_ms <= 0:
        parser.error('interval must be between 0.5 and 60 seconds, polls nonnegative, and stale-ms positive')
    client = OKXPublic(args.instrument, base_url=args.base_url)
    config = Config(order_size=args.order_size, max_inventory=args.max_inventory, fee_bps=args.fee_bps)
    execution = Execution(args.latency_ms, args.participation, args.queue_ahead)
    store = PaperStore(args.run_dir, {'version': 1, 'instrument': args.instrument,
                                    'config': asdict(config), 'execution': asdict(execution),
                                    'stale_ms': args.stale_ms, 'source': 'OKX public REST', 'base_url': args.base_url,
                                    'model': 'passive-trade-replay-v1; held prior REST book approximation'})
    runner = PaperRunner(store, args.instrument, config, execution, args.stale_ms)
    http = worker = None
    successful_polls = 0
    try:
        if not args.no_dashboard:
            from http.server import ThreadingHTTPServer
            from . import server
            server.engine = runner
            http = ThreadingHTTPServer(('127.0.0.1', args.port), server.Handler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            print(f'Paper dashboard: http://127.0.0.1:{args.port}', flush=True)
        print(f'Public data / simulated orders. Journal: {args.run_dir / "paper.sqlite3"}', flush=True)
        count = 0
        while args.polls == 0 or count < args.polls:
            accepted = poll_once(runner, client)
            count += 1
            successful_polls += int(accepted)
            print(json.dumps({'poll': count, 'accepted': accepted, 'feed': runner.snapshot()['feed']}), flush=True)
            if args.polls == 0 or count < args.polls:
                # Interruptible bounded backoff after feed faults.
                time.sleep(min(30, args.interval * (2 if not accepted else 1)))
    except KeyboardInterrupt:
        pass
    finally:
        if http:
            http.shutdown()
            http.server_close()
            worker.join()
        runner.close()
    if args.polls and not successful_polls:
        parser.exit(2, 'No fresh market data was accepted; inspect the journal and network connectivity.\n')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
