"""Local registry for reproducible offline study runs. No exchange or model calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import uuid

from .evaluate import digest, evaluate, verify_inputs


DEFAULT_REGISTRY = Path('data/experiments.sqlite3')


def now():
    return datetime.now(timezone.utc).isoformat()


def implementation_hash():
    root = Path(__file__).parent
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in ('engine.py', 'market_data.py', 'backtest.py', 'evaluate.py', 'experiments.py')})


def unpack(row):
    if row is None:
        return None

    result = dict(row)

    # Registries created under schema v1 contain only trading studies.
    result.setdefault("study_type", "trading")

    result["manifest"] = json.loads(result.pop("manifest_json"))
    result["summary"] = (
        json.loads(result["summary_json"])
        if result.get("summary_json")
        else None
    )
    result.pop("summary_json", None)

    return result


class Registry:
    def __init__(self, path=DEFAULT_REGISTRY):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row

        try:
            version = self.db.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

            tables = self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()

            table_names = {row[0] for row in tables}

            if version == 0 and not table_names:
                with self.db:
                    self.db.execute("""
                        CREATE TABLE runs (
                            id TEXT PRIMARY KEY,
                            fingerprint TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(
                                status IN (
                                    'running',
                                    'completed',
                                    'failed',
                                    'interrupted'
                                )
                            ),
                            created_at TEXT NOT NULL,
                            finished_at TEXT,
                            manifest_json TEXT NOT NULL,
                            implementation_sha256 TEXT NOT NULL,
                            python_version TEXT NOT NULL,
                            result_path TEXT,
                            result_sha256 TEXT,
                            summary_json TEXT,
                            error TEXT,
                            study_type TEXT NOT NULL DEFAULT 'trading'
                        )
                    """)

                    self.db.execute("""
                        CREATE UNIQUE INDEX one_active
                        ON runs(fingerprint)
                        WHERE status='running'
                    """)

                    self.db.execute("PRAGMA user_version=2")

            elif version == 1 and table_names == {"runs"}:
                # Existing registry: preserve all historical trading runs.
                with self.db:
                    columns = {
                        row[1]
                        for row in self.db.execute(
                            "PRAGMA table_info(runs)"
                        ).fetchall()
                    }

                    if "study_type" not in columns:
                        self.db.execute("""
                            ALTER TABLE runs
                            ADD COLUMN study_type TEXT NOT NULL
                            DEFAULT 'trading'
                        """)

                    self.db.execute("PRAGMA user_version=2")

            elif version == 2 and table_names == {"runs"}:
                columns = {
                    row[1]
                    for row in self.db.execute(
                        "PRAGMA table_info(runs)"
                    ).fetchall()
                }

                if "study_type" not in columns:
                    raise ValueError(
                        "Schema v2 registry is missing study_type"
                    )

            else:
                raise ValueError(
                    "Not a supported experiment registry"
                )

            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")

        except Exception:
            self.db.close()
            raise

    def start(
        self,
        fingerprint,
        manifest,
        code_hash,
        study_type="trading",
    ):
        if study_type not in ("trading", "dex_lp"):
            raise ValueError("Unsupported study_type")

        identifier = uuid.uuid4().hex

        try:
            with self.db:
                self.db.execute(
                    """
                    INSERT INTO runs (
                        id,
                        fingerprint,
                        status,
                        created_at,
                        manifest_json,
                        implementation_sha256,
                        python_version,
                        study_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        fingerprint,
                        "running",
                        now(),
                        json.dumps(manifest, allow_nan=False),
                        code_hash,
                        platform.python_version(),
                        study_type,
                    ),
                )

        except sqlite3.IntegrityError:
            raise ValueError(
                "An identical study is already marked running; "
                "inspect it before starting another"
            ) from None

        return identifier

    def finish(
        self,
        identifier,
        status,
        error=None,
        result_path=None,
        result_sha256=None,
        summary=None,
    ):
        if status not in ("completed", "failed", "interrupted"):
            raise ValueError("A terminal run status is required")

        with self.db:
            changed = self.db.execute(
                """
                UPDATE runs
                SET
                    status=?,
                    finished_at=?,
                    error=?,
                    result_path=?,
                    result_sha256=?,
                    summary_json=?
                WHERE id=? AND status=?
                """,
                (
                    status,
                    now(),
                    error,
                    str(result_path) if result_path else None,
                    result_sha256,
                    (
                        json.dumps(summary, allow_nan=False)
                        if summary is not None
                        else None
                    ),
                    identifier,
                    "running",
                ),
            ).rowcount

            if changed != 1:
                raise ValueError(
                    "Run is missing or is no longer running"
                )

    def close(self):
        self.db.close()
def read_runs(path=DEFAULT_REGISTRY, identifier=None, limit=50):
    path = Path(path)
    if not path.exists():
        return None if identifier else []
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        version = db.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

        if version not in (1, 2):
            raise ValueError("Not a supported experiment registry")
        if identifier:
            return unpack(db.execute('SELECT * FROM runs WHERE id=?', (identifier,)).fetchone())
        return [unpack(row) for row in db.execute('SELECT * FROM runs ORDER BY created_at DESC,id DESC LIMIT ?', (limit,))]
    finally:
        db.close()


def write_artifact(path, value):
    text = json.dumps(value, indent=2, allow_nan=False) + '\n'
    with Path(path).open('x', encoding='utf-8') as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
    return hashlib.sha256(text.encode()).hexdigest()


def run_study(manifest_path, registry_path=DEFAULT_REGISTRY, output_dir='data/experiments', repeat=False):
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise ValueError(f'Frozen study not found: {manifest_path}. Create one with app.evaluate freeze PLAN.json --output {manifest_path}, or use an existing *.lock.json file.')
    manifest = json.loads(manifest_path.read_text())
    code_hash = implementation_hash()
    fingerprint = digest({'manifest': manifest, 'implementation_sha256': code_hash, 'python_version': platform.python_version()})
    registry = Registry(registry_path)
    identifier = None
    try:
        try:
            verify_inputs(manifest)
        except Exception as error:
            identifier = registry.start(fingerprint, manifest, code_hash)
            registry.finish(identifier, 'failed', error=str(error))
            identifier = None
            raise
        if not repeat:
            row = registry.db.execute("SELECT * FROM runs WHERE fingerprint=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (fingerprint,)).fetchone()
            if row:
                result = unpack(row)
                raw = Path(result['result_path']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != result['result_sha256']:
                    raise ValueError('Completed result artifact changed; inspect it or use --repeat for a new attempt')
                return dict(result, reused=True)
        identifier = registry.start(fingerprint, manifest, code_hash)
        folder = Path(output_dir).resolve() / identifier
        folder.mkdir(parents=True, exist_ok=False)
        write_artifact(folder / 'manifest.lock.json', manifest)
        report = evaluate(manifest)
        report['experiment'] = {'id': identifier, 'fingerprint': fingerprint, 'implementation_sha256': code_hash}
        checksum = write_artifact(folder / 'report.json', report)
        registry.finish(identifier, 'completed', result_path=folder / 'report.json', result_sha256=checksum,
                        summary={'instrument': manifest['instrument'], 'results': len(report['results']),
                                 'roles_present': report['roles_present'], 'summaries': report['summaries']})
        identifier = None
        return dict(read_runs(registry_path, folder.name), reused=False)
    except BaseException as error:
        if identifier is not None:
            try:
                registry.finish(identifier, 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', error=str(error) or type(error).__name__)
            except (sqlite3.Error, ValueError):
                pass
        raise
    finally:
        registry.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('manifest', type=Path)
    run.add_argument('--output-dir', type=Path, default=Path('data/experiments'))
    run.add_argument('--repeat', action='store_true')
    listing = commands.add_parser('list')
    show = commands.add_parser('show')
    show.add_argument('id')
    interrupted = commands.add_parser('mark-interrupted', help='Record an already-stopped worker as interrupted')
    interrupted.add_argument('id')
    interrupted.add_argument('--reason', required=True)
    for command in (run, listing, show, interrupted):
        command.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    args = parser.parse_args()
    try:
        if args.command == 'run':
            result = run_study(args.manifest, args.registry, args.output_dir, args.repeat)
        elif args.command == 'mark-interrupted':
            registry = Registry(args.registry)
            try:
                registry.finish(args.id, 'interrupted', error=args.reason)
            finally:
                registry.close()
            result = read_runs(args.registry, args.id)
        else:
            result = read_runs(args.registry, getattr(args, 'id', None))
            if args.command == 'show' and result is None:
                raise ValueError('Experiment not found')
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Experiment command failed: {error}\n')


if __name__ == '__main__':
    main()
