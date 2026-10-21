import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.engine import Engine
from app.replay import replay
from app.storage import JournalError, PersistentEngine


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'simulation.sqlite3'

    def open(self):
        engine = PersistentEngine(self.path)
        self.addCleanup(engine.close)
        return engine

    def test_restart_continues_exact_random_sequence_and_full_state(self):
        reference = Engine()
        first = self.open()
        for _ in range(250):
            first.step()
            reference.step()
        restored = self.open()
        self.assertEqual(restored.snapshot(), reference.snapshot())
        for _ in range(101):
            restored.step()
            reference.step()
        self.assertEqual(restored.snapshot(), reference.snapshot())
        self.assertEqual(replay(self.path)['state'], reference.snapshot())

    def test_pause_survives_restart_and_is_audited(self):
        first = self.open()
        first.step()
        first.set_running(False)
        first.step()
        restored = self.open()
        self.assertFalse(restored.snapshot()['running'])
        self.assertEqual(restored.sequence, 2)
        restored.set_running(True)
        restored.step()
        self.assertEqual(replay(self.path)['verified_events'], 4)

    def test_commit_failure_leaves_state_and_rng_unchanged(self):
        engine = self.open()
        before = engine.snapshot()
        engine.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(JournalError):
            engine.step()
        self.assertEqual(engine.snapshot(), before)
        self.assertEqual(engine.sequence, 0)
        engine.db.execute('PRAGMA query_only=OFF')
        engine.step()
        reference = Engine()
        reference.step()
        self.assertEqual(engine.snapshot(), reference.snapshot())

    def test_stale_writer_is_rejected_without_state_mutation(self):
        first, second = self.open(), self.open()
        first.step()
        before = second.snapshot()
        with self.assertRaisesRegex(JournalError, 'another writer'):
            second.step()
        self.assertEqual(second.snapshot(), before)
        self.assertEqual(replay(self.path)['verified_events'], 1)

    def test_modified_event_is_detected(self):
        engine = self.open()
        engine.step()
        payload = json.loads(engine.db.execute('SELECT payload FROM events WHERE sequence=1').fetchone()[0])
        payload['state']['cash'] += 1
        with engine.db:
            engine.db.execute('UPDATE events SET payload=? WHERE sequence=1', (json.dumps(payload),))
        with self.assertRaisesRegex(JournalError, 'Replay mismatch'):
            replay(self.path)
        with self.assertRaisesRegex(JournalError, 'Replay mismatch'):
            PersistentEngine(self.path)

    def test_missing_replay_does_not_create_database(self):
        with self.assertRaises(sqlite3.OperationalError):
            replay(self.path)
        self.assertFalse(self.path.exists())

    def test_journal_includes_decision_quotes_and_fills(self):
        engine = self.open()
        for _ in range(100):
            engine.step()
        events = [json.loads(row[0]) for row in engine.db.execute('SELECT payload FROM events')]
        self.assertTrue(all('bid' in event['decision_quotes'] for event in events))
        self.assertTrue(any(event['fills'] for event in events))
        self.assertEqual(replay(self.path)['verified_events'], 100)


if __name__ == '__main__':
    unittest.main()
