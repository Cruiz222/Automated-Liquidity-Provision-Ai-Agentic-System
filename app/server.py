"""Run with python -m app.server; open http://127.0.0.1:8000."""
import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import urlsplit
from .storage import JournalError, PersistentEngine

engine = None
worker_error = None


def state():
    snapshot = engine.snapshot()
    snapshot['storage_error'] = worker_error
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
        if path == '/api/state':
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
        except JournalError:
            logging.exception('Could not persist agent control')
            self.respond(503, b'{"error":"Journal unavailable; control was not applied"}')
            return
        self.respond(200, json.dumps(state()).encode())


def main():
    global engine
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', default='data/simulation.sqlite3')
    args = parser.parse_args()
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
