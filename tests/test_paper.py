import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.error import URLError
from unittest.mock import patch

from app.backtest import Execution
from app.engine import Config
from app.market_data import load_csv
from app.okx_public import FeedError, OKXPublic, normalize
from app.paper import PaperRunner, poll_once
from app.paper_store import PaperStore, export_csv


def payload(book_ts, trades=(), bid=98, ask=102):
    return {'book': {'code': '0', 'data': [{'ts': str(book_ts), 'bids': [[str(bid), '10', '0', '1']],
                                           'asks': [[str(ask), '10', '0', '1']]}]},
            'trades': {'code': '0', 'data': [{'instId': 'BTC-USDT', 'tradeId': str(identifier),
                                             'ts': str(ts), 'px': str(price), 'sz': str(size), 'side': side}
                                            for identifier, ts, side, price, size in trades]}}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000
        self.store = PaperStore(self.root / 'run', {'version': 1})
        self.runner = PaperRunner(self.store, config=Config(half_spread_bps=100, inventory_skew_bps=0),
                                  execution=Execution(0, 1), stale_ms=5000, clock=lambda: self.now)
        self.addCleanup(self.runner.close)
        self.old = (1, 999, 'sell', 99, 1)

    def bootstrap(self):
        self.assertTrue(self.runner.ingest(payload(1000, [self.old])))
        self.assertEqual(self.runner.snapshot()['feed']['status'], 'synchronizing')
        self.assertEqual(self.runner.snapshot()['trades'], [])

    def test_bootstrap_dedup_and_live_partial_fills(self):
        self.bootstrap()
        self.now = 1200
        trade = (2, 1100, 'sell', 99, .4)
        self.assertTrue(self.runner.ingest(payload(1200, [trade, self.old, trade])))
        state = self.runner.snapshot()
        self.assertEqual(state['feed']['status'], 'healthy')
        self.assertEqual(state['inventory'], .4)
        self.assertEqual(len(state['trades']), 1)
        self.assertEqual(state['feed']['duplicates'], 2)
        self.assertTrue(self.runner.ingest(payload(1200, [trade, self.old])))
        self.assertEqual(self.runner.snapshot()['inventory'], .4)

    def test_quote_availability_uses_receipt_time_not_old_exchange_time(self):
        self.now = 1500
        self.runner.ingest(payload(1000, [self.old]))
        self.assertEqual(self.runner.session.pending['arrival_ms'], 1500)
        self.now = 1600
        self.runner.ingest(payload(1600, [self.old, (2, 1400, 'sell', 99, 1)]))
        self.assertEqual(self.runner.snapshot()['inventory'], 0)
        self.now = 1800
        self.runner.ingest(payload(1800, [(2, 1400, 'sell', 99, 1), (3, 1700, 'sell', 99, .4)]))
        self.assertEqual(self.runner.snapshot()['inventory'], .4)

    def test_missing_overlap_cancels_and_starts_new_segment_without_catchup_fills(self):
        self.bootstrap()
        self.now = 1200
        self.runner.ingest(payload(1200, [(9, 1150, 'sell', 99, 5)]))
        state = self.runner.snapshot()
        self.assertEqual(state['inventory'], 0)
        self.assertEqual(state['feed']['gaps'], 1)
        self.assertEqual(state['feed']['segment'], 2)
        self.assertEqual(state['quotes'], {'bid': None, 'ask': None})

    def test_stale_poll_and_watchdog_suspend_execution(self):
        self.bootstrap()
        self.now = 7000
        self.assertEqual(self.runner.snapshot()['feed']['status'], 'stale')
        self.assertFalse(self.runner.ingest(payload(1000, [self.old])))
        self.assertEqual(self.runner.session.active, {})
        self.assertIsNone(self.runner.session.pending)
        self.assertEqual(self.runner.snapshot()['inventory'], 0)
        self.assertTrue(self.runner.ingest(payload(7000, [(2, 6999, 'sell', 99, 1)])))
        self.assertEqual(self.runner.snapshot()['feed']['segment'], 2)

    def test_long_poll_gap_skips_potential_stale_fills(self):
        self.bootstrap()
        self.now = 7000
        self.runner.ingest(payload(7000, [self.old, (2, 6990, 'sell', 99, 1)]))
        self.assertEqual(self.runner.snapshot()['inventory'], 0)
        self.assertEqual(self.runner.snapshot()['feed']['gaps'], 1)

    def test_disconnect_recovery_and_pause_resume(self):
        self.bootstrap()
        client = type('Broken', (), {'poll': lambda _: (_ for _ in ()).throw(URLError('offline'))})()
        self.assertFalse(poll_once(self.runner, client))
        self.assertEqual(self.runner.snapshot()['feed']['status'], 'disconnected')
        self.runner.set_running(False)
        self.now = 1200
        self.runner.ingest(payload(1200, [self.old, (2, 1199, 'sell', 99, 1)]))
        self.assertIsNone(self.runner.session.pending)
        self.runner.set_running(True)
        self.now = 1400
        self.runner.ingest(payload(1400, [(2, 1199, 'sell', 99, 1), (3, 1399, 'sell', 99, 1)]))
        self.assertEqual(self.runner.snapshot()['inventory'], 0)
        self.assertEqual(self.runner.snapshot()['feed']['status'], 'synchronizing')

    def test_late_trade_regression_and_malformed_data(self):
        self.bootstrap()
        self.now = 1200
        self.runner.ingest(payload(1200, [self.old, (2, 999, 'sell', 99, 1)]))
        self.assertEqual(self.runner.snapshot()['inventory'], 0)
        self.assertFalse(self.runner.ingest(payload(1100, [(2, 999, 'sell', 99, 1)])))
        self.assertFalse(self.runner.ingest({'bad': 'data'}))
        self.assertEqual(self.runner.session.active, {})

    def test_commit_failure_never_publishes_paper_fill(self):
        self.bootstrap()
        self.now = 1200
        before = self.runner.snapshot()['cash']
        with patch.object(self.store, 'append', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'journal failed'):
                self.runner.ingest(payload(1200, [self.old, (2, 1100, 'sell', 99, 1)]))
        self.assertEqual(self.runner.snapshot()['cash'], before)
        self.assertEqual(self.runner.snapshot()['inventory'], 0)
        self.assertEqual(self.runner.snapshot()['feed']['status'], 'storage_failed')
        with self.assertRaises(RuntimeError):
            self.runner.set_running(True)

    def test_exports_valid_csv_segments_and_retains_raw_audit(self):
        self.bootstrap()
        self.now = 1200
        self.runner.ingest(payload(1200, [self.old, (2, 1100, 'sell', 99, .4)]))
        self.now = 1400
        self.runner.ingest(payload(1400, [(9, 1399, 'sell', 99, 1)]))
        counts = export_csv(self.root / 'run' / 'paper.sqlite3', self.root / 'export')
        self.assertEqual(counts, {1: 3, 2: 1})
        events = load_csv(self.root / 'export' / 'market-0001.csv')
        self.assertEqual(events[1].trade_size, .4)
        batches = [json.loads(row[0]) for row in self.store.db.execute('SELECT payload FROM batches ORDER BY sequence')]
        self.assertIsNotNone(batches[0]['raw'])
        self.assertEqual(batches[1]['events'][0]['fills'][0]['size'], .4)
        with self.assertRaises(FileExistsError):
            export_csv(self.root / 'run' / 'paper.sqlite3', self.root / 'export')

    def test_new_run_never_overwrites_existing_data(self):
        with self.assertRaises(FileExistsError):
            PaperStore(self.root / 'run', {})


class AdapterTests(unittest.TestCase):
    def test_invalid_payload_and_conflicting_duplicates(self):
        trade = (1, 1000, 'sell', 99, 1)
        bad = payload(1000, [trade, (1, 1000, 'sell', 98, 1)])
        with self.assertRaises(FeedError):
            normalize(bad, 'BTC-USDT')
        bad = payload(1000, [trade])
        bad['trades']['data'][0]['instId'] = 'ETH-USDT'
        with self.assertRaises(FeedError):
            normalize(bad, 'BTC-USDT')

    def test_transport_requests_only_public_get_endpoints(self):
        calls = []
        class Response:
            def __enter__(self):
                import io
                return io.StringIO('{"code":"0","data":[]}')
            def __exit__(self, *args):
                return False
        def opener(request, timeout):
            calls.append(request)
            return Response()
        OKXPublic(opener=opener).poll()
        self.assertTrue(all(request.get_method() == 'GET' for request in calls))
        self.assertTrue(calls[0].full_url.startswith('https://openapi.okx.com/api/v5/market/books?'))
        self.assertIn('/api/v5/market/trades?', calls[1].full_url)
        self.assertTrue(all(not any('key' in key.lower() for key in request.headers) for request in calls))
        with self.assertRaises(ValueError):
            OKXPublic('BTC-USDT-SWAP')


class DashboardIntegrationTests(unittest.TestCase):
    setUp = PaperTests.setUp
    bootstrap = PaperTests.bootstrap
    def request(self, method, path):
        import io
        from types import SimpleNamespace
        from app import server
        class Socket:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *args):
                return io.BytesIO(f'{method} {path} HTTP/1.0\r\nHost: localhost\r\n\r\n'.encode())
            def sendall(self, data):
                self.output.extend(data)
        connection = Socket()
        with patch.object(server, 'engine', self.runner), patch.object(server.Handler, 'log_message'):
            server.Handler(connection, ('127.0.0.1', 1234), SimpleNamespace())
        headers, body = bytes(connection.output).split(b'\r\n\r\n', 1)
        return headers, body

    def test_dashboard_state_controls_and_storage_fault(self):
        self.bootstrap()
        headers, body = self.request('GET', '/api/state')
        self.assertIn(b'200 OK', headers)
        self.assertEqual(json.loads(body)['mode'], 'paper')
        self.assertEqual(json.loads(body)['feed']['status'], 'synchronizing')
        _, body = self.request('POST', '/api/pause')
        self.assertFalse(json.loads(body)['running'])
        _, body = self.request('POST', '/api/resume')
        self.assertTrue(json.loads(body)['running'])
        self.runner.storage_error = 'disk full'
        with patch('app.server.logging.exception'):
            headers, _ = self.request('POST', '/api/pause')
        self.assertIn(b'503', headers)
        _, body = self.request('GET', '/api/state')
        self.assertEqual(json.loads(body)['storage_error'], 'disk full')
        headers, body = self.request('GET', '/')
        self.assertIn(b'200 OK', headers)
        self.assertIn(b'feed-health', body)
