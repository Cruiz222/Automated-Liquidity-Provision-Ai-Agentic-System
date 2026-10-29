"""Read-only OKX public REST requests. No credentials or order endpoints."""
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .market_data import MarketEvent


DEFAULT_BASE_URL = 'https://openapi.okx.com'
BASE_URLS = (DEFAULT_BASE_URL, 'https://www.okx.com', 'https://us.okx.com',
             'https://eea.okx.com', 'https://tr.okx.com')


class FeedError(ValueError):
    pass


class OKXPublic:
    def __init__(self, instrument='BTC-USDT', timeout=5, opener=None, base_url=DEFAULT_BASE_URL, dns_mode='system'):
        if not re.fullmatch(r'[A-Z0-9]+-USDT', instrument):
            raise ValueError('Use a USDT spot instrument such as BTC-USDT')
        if base_url not in BASE_URLS:
            raise ValueError('base_url must be an official supported OKX HTTPS endpoint')
        if dns_mode not in ('system', 'doh'):
            raise ValueError('dns_mode must be system or doh')
        if opener is None and dns_mode == 'doh':
            from .doh import DoHOpener
            opener = DoHOpener(urlsplit(base_url).hostname)
        self.dns_mode = dns_mode
        self.base_url = base_url
        self.instrument = instrument
        self.timeout = timeout
        self.opener = opener or urlopen

    def _get(self, path, **params):
        request = Request(self.base_url + path + '?' + urlencode(params),
                          headers={'User-Agent': 'LiquidityLab/0.1', 'Accept': 'application/json'})
        host = urlsplit(self.base_url).hostname
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise FeedError(f'OKX HTTP {error.code} from {host}; check service availability and regional endpoint configuration') from error
        except (URLError, OSError) as error:
            reason = error.reason if isinstance(error, URLError) else error
            if isinstance(reason, socket.gaierror):
                raise FeedError(f'DNS lookup failed for {host}; check DNS and network access. Run python3 -m app.network_check') from error
            if isinstance(reason, TimeoutError):
                raise FeedError(f'Connection timed out contacting {host}; check network access and service availability') from error
            raise FeedError(f'Cannot reach {host}: {reason}') from error
        if not isinstance(payload, dict) or payload.get('code') != '0' or not isinstance(payload.get('data'), list):
            raise FeedError('Invalid or unsuccessful OKX response')
        return payload

    def poll(self):
        # Snapshot first: subsequent trades can be aligned with this or an older book.
        book = self._get('/api/v5/market/books', instId=self.instrument, sz=1)
        trades = self._get('/api/v5/market/trades', instId=self.instrument, limit=500)
        return {'book': book, 'trades': trades}


def normalize(payload, instrument):
    try:
        for key in ('book', 'trades'):
            if payload[key]['code'] != '0':
                raise FeedError('OKX returned an error')
        books = payload['book']['data']
        if len(books) != 1:
            raise FeedError('Expected one book snapshot')
        book = books[0]
        snapshot = MarketEvent(int(book['ts']), float(book['bids'][0][0]), float(book['asks'][0][0]),
                               float(book['bids'][0][1]), float(book['asks'][0][1]))
        trades = []
        seen = {}
        duplicates = 0
        for trade in payload['trades']['data']:
            if trade['instId'] != instrument:
                raise FeedError('Unexpected trade instrument')
            identifier = int(trade['tradeId'])
            if identifier < 0:
                raise FeedError('Invalid trade identifier')
            event = MarketEvent(int(trade['ts']), snapshot.bid, snapshot.ask,
                                snapshot.bid_size, snapshot.ask_size,
                                float(trade['px']), float(trade['sz']), trade['side'])
            if identifier in seen:
                if seen[identifier] != event:
                    raise FeedError('Conflicting duplicate trade identifier')
                duplicates += 1
                continue
            seen[identifier] = event
            trades.append((identifier, event))
        trades.sort(key=lambda item: (item[1].timestamp_ms, item[0]))
        if any(a[0] >= b[0] for a, b in zip(trades, trades[1:])):
            raise FeedError('Trade identifiers and timestamps disagree')
        return snapshot, trades, duplicates
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        raise FeedError(f'Malformed market payload: {error}') from error
