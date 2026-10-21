"""SQLite event journal and deterministic recovery for the simulation engine."""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import threading

from .engine import Config, Engine


class JournalError(RuntimeError):
    """The journal cannot safely accept or recover simulation events."""


def event_result(engine, kind):
    state = engine.snapshot()
    return {
        'state': {key: value for key, value in state.items() if key not in ('trades', 'history', 'config')},
        'fills': [trade for trade in state['trades'] if trade['tick'] == state['tick']] if kind == 'tick' else [],
    }


class PersistentEngine:
    """Publish state only after its event commits; reject concurrent writers."""

    def __init__(self, path, config=None, seed=7):
        self.lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO metadata VALUES (1, ?)',
                                (json.dumps({'version': 1, 'seed': seed, 'config': asdict(config or Config())}),))
            metadata = json.loads(self.db.execute('SELECT payload FROM metadata WHERE id=1').fetchone()[0])
            if metadata['version'] != 1:
                raise JournalError('Unsupported journal version')
            if config is not None and asdict(config) != metadata['config']:
                raise JournalError('Configuration differs from the existing journal')
            self.engine = Engine(Config(**metadata['config']), metadata['seed'])
            self.sequence = 0
            for sequence, kind, payload in self.db.execute('SELECT sequence, kind, payload FROM events ORDER BY sequence'):
                if sequence != self.sequence + 1:
                    raise JournalError('Journal sequence gap')
                before = self.engine.quotes()
                self._apply(self.engine, kind)
                expected = event_result(self.engine, kind)
                expected['decision_quotes'] = before if kind == 'tick' else None
                if expected != json.loads(payload):
                    raise JournalError(f'Replay mismatch at event {sequence}')
                self.sequence = sequence
        except Exception:
            self.db.close()
            raise

    @staticmethod
    def _apply(engine, kind):
        if kind == 'tick':
            if not engine.running:
                raise JournalError('Tick recorded while paused')
            engine.step()
        elif kind in ('pause', 'resume'):
            engine.set_running(kind == 'resume')
        else:
            raise JournalError(f'Unknown event type: {kind}')

    def _commit(self, kind):
        # A candidate owns its mutable state, including the PRNG's cached Gaussian.
        candidate = copy.copy(self.engine)
        candidate.lock = threading.RLock()
        candidate.rng = copy.deepcopy(self.engine.rng)
        candidate.trades = list(self.engine.trades)
        candidate.history = list(self.engine.history)
        before = candidate.quotes()
        self._apply(candidate, kind)
        payload = event_result(candidate, kind)
        payload['decision_quotes'] = before if kind == 'tick' else None
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                latest = self.db.execute('SELECT COALESCE(MAX(sequence), 0) FROM events').fetchone()[0]
                if latest != self.sequence:
                    raise JournalError('Journal changed by another writer; restart to recover')
                self.db.execute('INSERT INTO events VALUES (?, ?, ?)',
                                (self.sequence + 1, kind, json.dumps(payload, allow_nan=False)))
        except sqlite3.Error as error:
            raise JournalError(f'Event commit failed: {error}') from error
        self.engine = candidate
        self.sequence += 1

    def step(self):
        with self.lock:
            if self.engine.running:
                self._commit('tick')

    def set_running(self, running):
        with self.lock:
            if self.engine.running != running:
                self._commit('resume' if running else 'pause')

    def snapshot(self):
        with self.lock:
            return self.engine.snapshot()

    def close(self):
        with self.lock:
            self.db.close()
