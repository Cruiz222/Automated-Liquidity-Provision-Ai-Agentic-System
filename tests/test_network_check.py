import socket
import unittest
from urllib.error import HTTPError, URLError

from app.network_check import check
from app.okx_public import DEFAULT_BASE_URL, FeedError, OKXPublic


def resolver(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443))]


class NetworkTests(unittest.TestCase):
    def test_documented_endpoint_and_explicit_endpoint_validation(self):
        self.assertEqual(OKXPublic().base_url, 'https://openapi.okx.com')
        self.assertEqual(OKXPublic(base_url='https://www.okx.com').base_url, 'https://www.okx.com')
        for endpoint in ('http://openapi.okx.com', 'https://attacker.invalid', 'https://openapi.okx.com/path'):
            with self.assertRaises(ValueError):
                OKXPublic(base_url=endpoint)

    def test_dns_failure_is_actionable_and_retains_cause(self):
        original = URLError(socket.gaierror(-3, 'Temporary failure in name resolution'))
        def opener(*args, **kwargs):
            raise original
        client = OKXPublic(opener=opener)
        with self.assertRaisesRegex(FeedError, 'DNS lookup failed for openapi.okx.com') as context:
            client.poll()
        self.assertIs(context.exception.__cause__, original)
        self.assertIn('app.network_check', str(context.exception))

    def test_http_failure_is_not_misreported_as_dns(self):
        def opener(*args, **kwargs):
            raise HTTPError(DEFAULT_BASE_URL, 403, 'Forbidden', {}, None)
        with self.assertRaisesRegex(FeedError, 'HTTP 403'):
            OKXPublic(opener=opener).poll()

    def test_dns_failure_skips_api_requests(self):
        class Client:
            def poll(self):
                raise AssertionError('API must not be contacted')
        def broken(*args, **kwargs):
            raise socket.gaierror(-3, 'DNS unavailable')
        report = check(resolver=broken, client=Client())
        self.assertEqual(report['dns'], 'failed')
        self.assertEqual(report['public_api'], 'unchecked')

    def test_dns_success_and_api_failure_report_distinct_stages(self):
        class Client:
            def poll(self):
                raise FeedError('Connection timed out')
        report = check(resolver=resolver, client=Client())
        self.assertEqual(report['dns'], 'ok')
        self.assertEqual(report['public_api'], 'failed')

    def test_success_checks_both_public_payloads(self):
        class Client:
            def poll(self):
                return {'book': {'code': '0', 'data': [{'ts': '1000', 'bids': [['99', '1']], 'asks': [['101', '1']]}]},
                        'trades': {'code': '0', 'data': []}}
        report = check(resolver=resolver, client=Client())
        self.assertEqual(report['public_api'], 'ok')
        self.assertEqual(report['book_timestamp_ms'], 1000)
