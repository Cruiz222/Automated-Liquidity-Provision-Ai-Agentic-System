"""Public OKX WebSocket transport; optional pinned websockets dependency."""
import json
import re
import socket
import ssl
import time

from .doh import Resolver
from .okx_public import FeedError


URL = 'wss://ws.okx.com/ws/v5/public'


class OKXStream:
    def __init__(self, instrument='BTC-USDT', dns_mode='system', connect=None,
                 clock=time.monotonic, wall_clock=lambda: int(time.time() * 1000)):
        if not re.fullmatch(r'[A-Z0-9]+-USDT', instrument) or dns_mode not in ('system', 'doh'):
            raise ValueError('Use a USDT spot instrument and system or doh DNS mode')
        if connect is None:
            try:
                from websockets.sync.client import connect
            except ImportError:
                raise ValueError('WebSocket mode needs the optional dependency. Run .venv/bin/python -m pip install -r requirements-websocket.txt, then use .venv/bin/python -m app.paper') from None
        self.connect, self.instrument, self.dns_mode = connect, instrument, dns_mode
        self.clock, self.wall_clock = clock, wall_clock
        self.connection = None
        self.epoch = 0
        self.subscribed = set()
        self.resolver = Resolver('ws.okx.com') if dns_mode == 'doh' else None
        self.last_received = self.last_ping = 0

    def open(self):
        raw = self.resolver.create_connection(('ws.okx.com', 443), timeout=5) if self.resolver else None
        try:
            self.connection = self.connect(URL, sock=raw, ssl=ssl.create_default_context(),
                server_hostname='ws.okx.com', proxy=None, open_timeout=5, close_timeout=2,
                ping_interval=20, ping_timeout=10, max_size=1_048_576, max_queue=256)
            self.connection.send(json.dumps({'op': 'subscribe', 'args': [
                {'channel': 'books5', 'instId': self.instrument},
                {'channel': 'trades', 'instId': self.instrument}]}))
        except Exception:
            if raw is not None:
                raw.close()
            self.close()
            raise
        self.epoch += 1
        self.subscribed = set()
        self.opened = self.last_received = self.clock()
        self.last_ping = 0

    def poll(self):
        try:
            if self.connection is None:
                self.open()
            messages = []
            deadline = self.clock() + .5
            while self.clock() < deadline:
                try:
                    text = self.connection.recv(timeout=min(.1, max(.001, deadline - self.clock())))
                except TimeoutError:
                    continue
                if not isinstance(text, str):
                    raise FeedError('Unexpected binary WebSocket message')
                self.last_received = self.clock()
                payload = 'pong' if text == 'pong' else json.loads(text)
                if isinstance(payload, dict) and payload.get('event') == 'subscribe':
                    arg = payload.get('arg', {})
                    if arg.get('instId') != self.instrument or arg.get('channel') not in ('books5', 'trades'):
                        raise FeedError('Unexpected subscription acknowledgement')
                    self.subscribed.add(arg['channel'])
                messages.append({'received_ms': self.wall_clock(), 'payload': payload})
                if len(messages) > 2000:
                    raise FeedError('WebSocket batch capacity exceeded; resynchronization required')
            if self.clock() - self.opened > 5 and self.subscribed != {'books5', 'trades'}:
                raise FeedError('WebSocket subscriptions were not acknowledged')
            if self.clock() - self.last_received > 15 and self.clock() - self.last_ping > 5:
                self.connection.send('ping')
                self.last_ping = self.clock()
            if self.clock() - self.last_received > 25:
                raise FeedError('WebSocket application heartbeat timed out')
            return {'transport': 'websocket', 'epoch': self.epoch, 'messages': messages}
        except Exception as error:
            self.close()
            raise FeedError(f'WebSocket transport failed: {error}') from error

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
