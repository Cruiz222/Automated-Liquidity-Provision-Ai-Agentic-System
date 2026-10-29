import io
import json
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.request import Request

from app.doh import Connection, DoHOpener, Handler, NoRedirect, Resolver
from app.okx_public import OKXPublic


class DoHTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.payload = {'Status': 0, 'Answer': [
            {'name': 'openapi.okx.com', 'type': 5, 'TTL': 10, 'data': 'edge.example.'},
            {'name': 'edge.example.', 'type': 1, 'TTL': 60, 'data': '1.1.1.1'},
            {'name': 'unrelated.example', 'type': 1, 'TTL': 60, 'data': '8.8.8.8'}]}
        self.calls = []
        def opener(request, timeout):
            self.calls.append(request)
            return io.StringIO(json.dumps(self.payload))
        self.resolver = Resolver('openapi.okx.com', opener=opener, clock=lambda: self.now)

    def test_cache_respects_cname_ttl_and_query_uses_https(self):
        self.assertEqual(self.resolver.resolve('openapi.okx.com'), ['1.1.1.1'])
        self.now = 9
        self.resolver.resolve('openapi.okx.com')
        self.assertEqual(len(self.calls), 1)
        self.now = 10
        self.resolver.resolve('openapi.okx.com')
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0].full_url.startswith('https://1.1.1.1/dns-query?'))
        self.assertEqual(self.calls[0].get_header('Accept'), 'application/dns-json')

    def test_negative_response_and_private_addresses_fail_closed(self):
        for payload in ({'Status': 5}, {'Status': 0}, {'Status': 0, 'Answer': 'invalid'},
                        {'Status': 0, 'Answer': [{'name': 'openapi.okx.com', 'type': 1, 'TTL': 5, 'data': '127.0.0.1'}]},
                        {'Status': 0, 'Answer': [{'name': 'openapi.okx.com', 'type': 1, 'TTL': -1, 'data': '1.1.1.1'}]}):
            self.payload = payload
            self.resolver.cached = []
            with self.subTest(payload=payload), self.assertRaises(URLError):
                self.resolver.resolve('openapi.okx.com')

    def test_tls_uses_original_hostname_with_verified_context(self):
        transport = Handler(self.resolver)
        self.assertTrue(transport._context.check_hostname)
        self.assertEqual(transport._context.verify_mode, ssl.CERT_REQUIRED)
        context = MagicMock()
        sock = MagicMock()
        with patch('app.doh.socket.create_connection', return_value=sock) as connect:
            connection = Connection('openapi.okx.com', resolver=self.resolver, context=context, timeout=5)
            connection.connect()
        connect.assert_called_once_with(('1.1.1.1', 443), 5, None)
        context.wrap_socket.assert_called_once_with(sock, server_hostname='openapi.okx.com')
        self.assertEqual(connection.host, 'openapi.okx.com')

    def test_failed_connections_invalidate_cache(self):
        with patch('app.doh.socket.create_connection', side_effect=TimeoutError('network timeout')):
            with self.assertRaises(TimeoutError):
                self.resolver.create_connection(('openapi.okx.com', 443))
        self.assertEqual(self.resolver.cached, [])

    def test_hosts_ports_schemes_and_redirects_are_restricted(self):
        with self.assertRaises(URLError):
            self.resolver.resolve('unexpected.example')
        with self.assertRaises(URLError):
            self.resolver.create_connection(('openapi.okx.com', 80))
        opener = DoHOpener('openapi.okx.com')
        for url in ('http://openapi.okx.com/', 'https://example.com/', 'https://openapi.okx.com:8443/'):
            with self.subTest(url=url), self.assertRaises(URLError):
                opener(Request(url))
        with self.assertRaises(URLError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')

    def test_mode_is_opt_in_without_global_socket_patching(self):
        original = socket.getaddrinfo
        system = OKXPublic()
        doh = OKXPublic(dns_mode='doh')
        self.assertEqual(system.dns_mode, 'system')
        self.assertIsInstance(doh.opener, DoHOpener)
        self.assertIs(socket.getaddrinfo, original)
        with self.assertRaises(ValueError):
            OKXPublic(dns_mode='invalid')
