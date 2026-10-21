"""Run with python -m app.server; open http://127.0.0.1:8000."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import urlsplit
from .engine import Engine

engine = Engine()


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
            self.respond(200, json.dumps(engine.snapshot()).encode())
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
        engine.set_running(self.path == '/api/resume')
        self.respond(200, json.dumps(engine.snapshot()).encode())


def main():
    stop = threading.Event()
    def simulate():
        while not stop.wait(1):
            engine.step()
    worker = threading.Thread(target=simulate, daemon=True)
    worker.start()
    server = ThreadingHTTPServer(('127.0.0.1', 8000), Handler)
    print('Liquidity Lab: http://127.0.0.1:8000 (simulation only)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join()


if __name__ == '__main__':
    main()
