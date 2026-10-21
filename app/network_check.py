"""Diagnose DNS and public OKX connectivity without creating a paper run."""
import argparse
import json
import socket
from urllib.parse import urlsplit

from .okx_public import BASE_URLS, DEFAULT_BASE_URL, FeedError, OKXPublic, normalize


def check(base_url=DEFAULT_BASE_URL, instrument='BTC-USDT', resolver=socket.getaddrinfo, client=None):
    host = urlsplit(base_url).hostname
    report = {'base_url': base_url, 'instrument': instrument, 'dns': 'unchecked',
              'public_api': 'unchecked', 'error': None}
    client = client or OKXPublic(instrument, base_url=base_url)
    try:
        addresses = resolver(host, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise socket.gaierror('No addresses returned')
        report['dns'] = 'ok'
    except OSError as error:
        report.update(dns='failed', error=f'DNS lookup failed for {host}: {error}')
        return report
    try:
        book, trades, _ = normalize(client.poll(), instrument)
        report.update(public_api='ok', book_timestamp_ms=book.timestamp_ms, trades_received=len(trades))
    except (FeedError, OSError, ValueError) as error:
        report.update(public_api='failed', error=str(error))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', choices=BASE_URLS, default=DEFAULT_BASE_URL)
    parser.add_argument('--instrument', default='BTC-USDT')
    args = parser.parse_args()
    report = check(args.base_url, args.instrument)
    print(json.dumps(report, indent=2))
    if report['public_api'] != 'ok':
        parser.exit(1)


if __name__ == '__main__':
    main()
