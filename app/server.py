"""Run with python -m app.server; open http://127.0.0.1:8000."""
import argparse
import json
import logging
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import urlsplit
from .storage import JournalError, PersistentEngine
from .experiments import DEFAULT_REGISTRY, read_runs
from .operations import snapshot as operations_snapshot

engine = None
worker_error = None
registry_path = DEFAULT_REGISTRY
data_dir = Path('data')


def state():
    snapshot = engine.snapshot()
    snapshot['storage_error'] = worker_error or snapshot.get('storage_error')
    return snapshot


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/operations':
            try:
                result = operations_snapshot(data_dir, registry_path, state() if engine is not None else None)
                self.respond(200, json.dumps(result).encode())
            except (OSError, ValueError, KeyError, TypeError):
                self.respond(503, b'{"error":"Operations summary unavailable"}')
        elif path == '/api/experiments' or path.startswith('/api/experiments/'):
            identifier = path.removeprefix('/api/experiments/') if path != '/api/experiments' else None
            if identifier is not None and not re.fullmatch(r'[0-9a-f]{32}', identifier):
                self.respond(404, b'{"error":"Experiment not found"}')
                return
            try:
                result = read_runs(registry_path, identifier)
            except (OSError, sqlite3.Error, ValueError):
                self.respond(503, b'{"error":"Experiment registry unavailable"}')
                return
            if identifier is not None and result is None:
                self.respond(404, b'{"error":"Experiment not found"}')
                return
            self.respond(200, json.dumps(result).encode())
        elif path == '/api/state':
            self.respond(200, json.dumps(state()).encode())
        elif path == '/':
            self.respond(200, Path(__file__).with_name('dashboard.html').read_bytes(), 'text/html; charset=utf-8')
        else:
            self.respond(404, b'{"error":"Not found"}')

    def do_POST(self):
        # Browser controls must originate from this local server.
        if self.headers.get('Origin') not in (None, 'http://' + self.headers.get('Host', '')):
            self.respond(403, b'{"error":"Invalid origin"}')
            return
        if self.path not in ('/api/pause', '/api/resume'):
            self.respond(404, b'{"error":"Not found"}')
            return
        try:
            engine.set_running(self.path == '/api/resume')
        except (JournalError, RuntimeError):
            logging.exception('Could not persist agent control')
            self.respond(503, b'{"error":"Journal unavailable; control was not applied"}')
            return
        self.respond(200, json.dumps(state()).encode())


def main():
    global engine, registry_path, data_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument('--journal', default='data/simulation.sqlite3')
    args = parser.parse_args()
    registry_path = args.registry
    data_dir = args.data_dir
    engine = PersistentEngine(args.journal)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 8000), Handler)
    except OSError:
        engine.close()
        raise
    stop = threading.Event()
    def simulate():
        global worker_error
        while not stop.wait(1):
            try:
                engine.step()
                worker_error = None
            except JournalError:
                worker_error = 'Journal unavailable. Simulation is stalled; retrying without advancing state.'
                logging.exception(worker_error)
    worker = threading.Thread(target=simulate, daemon=True)
    worker.start()
    print('Liquidity Lab: http://127.0.0.1:8000 (simulation only)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join()
        engine.close()


if __name__ == '__main__':
    main()
