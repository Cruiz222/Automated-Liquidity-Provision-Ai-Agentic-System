from dataclasses import asdict
import copy
import json
from pathlib import Path
import sqlite3
import ssl
import tempfile
import unittest
from unittest.mock import patch

from app.backtest import Execution
from app.engine import Config
from app.okx_stream import OKXStream
from app.paper_store import PaperStore, export_csv
from app.paper_report import audit
from app.stream_paper import StreamPaperRunner


def book(t, sequence, bid=98, ask=102):
    return {'arg': {'channel': 'books5', 'instId': 'BTC-USDT'}, 'data': [
        {'ts': str(t), 'seqId': sequence, 'bids': [[str(bid), '10']], 'asks': [[str(ask), '10']]}]}


def trade(t, identifier, price=99, size=.4):
    return {'arg': {'channel': 'trades', 'instId': 'BTC-USDT'}, 'data': [
        {'ts': str(t), 'tradeId': str(identifier), 'px': str(price), 'sz': str(size), 'side': 'sell'}]}


class StreamPaperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000
        config, execution = Config(half_spread_bps=100, inventory_skew_bps=0), Execution(0, 1)
        self.store = PaperStore(self.root / 'run', {'version': 1, 'instrument': 'BTC-USDT', 'config': asdict(config), 'execution': asdict(execution)})
        self.runner = StreamPaperRunner(self.store, config=config, execution=execution,
                                       clock=lambda: self.now, reorder_ms=100)
        self.addCleanup(self.runner.close)
        self.journal = self.root / 'run' / 'paper.sqlite3'

    def ingest(self, messages, epoch=1):
        return self.runner.ingest({'transport': 'websocket', 'epoch': epoch,
                                  'messages': [{'received_ms': self.now, 'payload': m} for m in messages]})

    def bootstrap(self):
        self.assertTrue(self.ingest([book(900, 1)]))
        self.assertEqual(self.runner.feed.segment, 1)

    def test_books_advance_without_new_trades_and_buffer_flushes_on_clock(self):
        self.bootstrap()
        self.now = 1200
        self.ingest([book(1150, 2)])
        self.assertEqual(self.runner.session.previous.timestamp_ms, 900)
        self.now = 1300
        self.ingest([])
        self.assertEqual(self.runner.session.previous.timestamp_ms, 1150)
        self.assertEqual(self.runner.health()['buffered_events'], 0)
        self.assertEqual(self.runner.health()['transport'], 'OKX public WebSocket')

    def test_reorder_book_and_trade_then_deduplicate_without_double_fill(self):
        self.bootstrap()
        self.now = 1300
        self.ingest([trade(1150, 100), book(1100, 2)])
        self.assertEqual(self.runner.session.account.inventory, .4)
        self.now = 1400
        self.ingest([trade(1150, 100)])
        self.assertEqual(self.runner.session.account.inventory, .4)
        self.assertEqual(self.runner.feed.duplicates, 1)
        self.assertTrue(audit(self.journal)['accounting_verified'])

    def test_reconnect_cancels_orders_and_starts_new_segment(self):
        self.bootstrap()
        self.now = 1300
        self.ingest([book(1200, 1)], epoch=2)
        self.assertEqual(self.runner.feed.segment, 2)
        self.assertEqual(self.runner.feed.gaps, 1)
        self.assertEqual(self.runner.session.account.inventory, 0)

    def test_late_event_fails_closed_and_clears_reorder_buffer(self):
        self.bootstrap()
        self.now = 1400
        self.ingest([book(1250, 2)])
        self.now = 1500
        self.assertFalse(self.ingest([trade(1100, 2)]))
        self.assertEqual(self.runner.feed.buffer, [])
        self.assertEqual(self.runner.session.active, {})
        self.assertIsNone(self.runner.session.pending)
        self.assertEqual(self.runner.session.account.inventory, 0)

    def test_no_retroactive_fills_and_pause_resume(self):
        self.now = 1500
        self.ingest([book(1000, 1), trade(1200, 1)])
        self.assertEqual(self.runner.session.account.inventory, 0)
        self.runner.set_running(False)
        self.now = 1700
        self.ingest([book(1600, 2), trade(1600, 2)])
        self.assertEqual(self.runner.session.account.inventory, 0)
        self.runner.set_running(True)
        self.now = 1900
        self.ingest([book(1800, 3)])
        self.assertEqual(self.runner.feed.segment, 2)

    def test_sequence_reset_and_invalid_instrument_require_resync(self):
        self.bootstrap()
        self.now = 1200
        self.assertFalse(self.ingest([book(1100, 0)]))
        wrong = book(1100, 2)
        wrong['arg']['instId'] = 'ETH-USDT'
        self.assertFalse(self.ingest([wrong]))

    def test_write_failure_does_not_publish_fills_or_dedup_state(self):
        self.bootstrap()
        self.now = 1300
        with patch.object(self.store, 'append', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(RuntimeError):
                self.ingest([trade(1150, 1)])
        self.assertEqual(self.runner.session.account.inventory, 0)
        self.assertEqual(self.runner.feed.recent_ids, [])

    def test_export_and_audit_use_same_existing_schema(self):
        self.bootstrap()
        self.now = 1300
        self.ingest([book(1100, 2), trade(1150, 1)])
        self.assertEqual(export_csv(self.journal, self.root / 'export'), {1: 3})
        self.assertEqual(audit(self.journal)['fill_count'], 1)


class StreamTransportTests(unittest.TestCase):
    def test_public_subscription_and_hostname_verification(self):
        class Connection:
            def __init__(self): self.sent = []
            def send(self, value): self.sent.append(json.loads(value))
            def close(self): pass
        connection = Connection()
        calls = []
        def connect(url, **kwargs):
            calls.append((url, kwargs))
            return connection
        client = OKXStream(connect=connect)
        client.open()
        try:
            self.assertEqual(calls[0][0], 'wss://ws.okx.com/ws/v5/public')
            options = calls[0][1]
            self.assertEqual(options['server_hostname'], 'ws.okx.com')
            self.assertTrue(options['ssl'].check_hostname)
            self.assertEqual(options['ssl'].verify_mode, ssl.CERT_REQUIRED)
            self.assertIsNone(options['proxy'])
            self.assertEqual(connection.sent[0]['op'], 'subscribe')
            self.assertEqual({a['channel'] for a in connection.sent[0]['args']}, {'books5', 'trades'})
        finally:
            client.close()


class StreamHeartbeatTests(unittest.TestCase):
    def test_heartbeat_timeout_closes_and_next_poll_creates_new_epoch(self):
        class Connection:
            def __init__(self): self.closed = False
            def send(self, value): pass
            def recv(self, timeout):
                clock[0] += timeout
                raise TimeoutError()
            def close(self): self.closed = True
        clock = [0.0]
        connections = []
        def connect(*args, **kwargs):
            connection = Connection()
            connections.append(connection)
            return connection
        client = OKXStream(connect=connect, clock=lambda: clock[0])
        client.open()
        client.subscribed = {'books5', 'trades'}
        clock[0] = 26
        with self.assertRaisesRegex(ValueError, 'heartbeat timed out'):
            client.poll()
        self.assertTrue(connections[0].closed)
        self.assertIsNone(client.connection)
        client.open()
        self.assertEqual(client.epoch, 2)
        client.close()
