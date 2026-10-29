"""Verify a journal without opening it for writes or creating a missing file."""
import argparse
import json
from pathlib import Path
import sqlite3

from .engine import Config, Engine
from .storage import JournalError, PersistentEngine, event_result


def replay(path):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True)
    try:
        # A consistent read transaction allows verification while the server runs.
        db.execute('BEGIN')
        metadata = json.loads(db.execute('SELECT payload FROM metadata WHERE id=1').fetchone()[0])
        if metadata['version'] != 1:
            raise JournalError('Unsupported journal version')
        engine = Engine(Config(**metadata['config']), metadata['seed'])
        count = 0
        for sequence, kind, payload in db.execute('SELECT sequence, kind, payload FROM events ORDER BY sequence'):
            if sequence != count + 1:
                raise JournalError('Journal sequence gap')
            before = engine.quotes()
            PersistentEngine._apply(engine, kind)
            expected = event_result(engine, kind)
            expected['decision_quotes'] = before if kind == 'tick' else None
            if expected != json.loads(payload):
                raise JournalError(f'Replay mismatch at event {sequence}')
            count = sequence
        return {'verified_events': count, 'state': engine.snapshot()}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', nargs='?', default='data/simulation.sqlite3')
    args = parser.parse_args()
    try:
        print(json.dumps(replay(args.journal), indent=2))
    except (JournalError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Replay failed: {error}\n')


if __name__ == '__main__':
    main()
