"""Opt-in, process-local Cloudflare DoH for a single allowed HTTPS host."""
from functools import partial
import http.client
import ipaddress
import json
import math
import socket
import ssl
import time
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError('Redirects are disabled for the direct DoH transport')


class Resolver:
    def __init__(self, host, opener=None, clock=time.monotonic):
        self.host = host.lower()
        self.clock = clock
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect(),
                                             HTTPSHandler(context=ssl.create_default_context())).open
        self.cached = []
        self.expires = 0

    def resolve(self, host, timeout=5):
        if host.lower() != self.host:
            raise URLError('DoH transport received an unexpected hostname')
        if self.cached and self.clock() < self.expires:
            return list(self.cached)
        self.cached = []
        self.expires = 0
        query = urlencode({'name': self.host, 'type': 'A'})
        request = Request('https://1.1.1.1/dns-query?' + query,
                          headers={'Accept': 'application/dns-json', 'User-Agent': 'LiquidityLab/0.1'})
        with self.opener(request, timeout=timeout) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or payload.get('Status') != 0:
            raise URLError('DNS-over-HTTPS resolver returned an unsuccessful response')
        answers = payload.get('Answer', [])
        if not isinstance(answers, list) or not all(isinstance(answer, dict) for answer in answers):
            raise URLError('Invalid DNS-over-HTTPS answer records')
        # Only accept A records belonging to the queried name or its CNAME chain.
        owner = self.host
        ttls = []
        for _ in range(8):
            aliases = [a for a in answers if a.get('type') == 5 and str(a.get('name', '')).lower().rstrip('.') == owner]
            if not aliases:
                break
            alias = aliases[0]
            ttls.append(float(alias.get('TTL', 0)))
            owner = str(alias['data']).lower().rstrip('.')
        addresses = []
        for answer in answers:
            if answer.get('type') != 1 or str(answer.get('name', '')).lower().rstrip('.') != owner:
                continue
            address = ipaddress.ip_address(answer['data'])
            if address.version != 4 or not address.is_global:
                raise URLError('DoH returned a non-public IPv4 address')
            addresses.append(str(address))
            ttls.append(float(answer.get('TTL', 0)))
        if not addresses or not all(math.isfinite(ttl) and ttl >= 0 for ttl in ttls):
            raise URLError('DoH returned no usable addresses or an invalid TTL')
        self.cached = list(dict.fromkeys(addresses))
        self.expires = self.clock() + min(300, min(ttls))
        return list(self.cached)

    def create_connection(self, address, timeout=5, source_address=None):
        host, port = address
        if port != 443:
            raise URLError('DoH transport only supports HTTPS port 443')
        errors = []
        for resolved in self.resolve(host, timeout):
            try:
                return socket.create_connection((resolved, port), timeout, source_address)
            except OSError as error:
                errors.append(error)
        self.cached = []
        self.expires = 0
        raise errors[-1]


class Connection(http.client.HTTPSConnection):
    def __init__(self, *args, resolver, **kwargs):
        super().__init__(*args, **kwargs)
        # HTTPSConnection still wraps the socket using the ORIGINAL hostname,
        # preserving certificate hostname verification, SNI and the Host header.
        self._create_connection = resolver.create_connection


class Handler(HTTPSHandler):
    def __init__(self, resolver):
        super().__init__(context=ssl.create_default_context())
        self.factory = partial(Connection, resolver=resolver)

    def https_open(self, request):
        return self.do_open(self.factory, request, context=self._context)


class DoHOpener:
    def __init__(self, host):
        self.resolver = Resolver(host)
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), Handler(self.resolver))

    def __call__(self, request, timeout=5):
        parsed = urlsplit(request.full_url)
        if parsed.scheme != 'https' or parsed.hostname != self.resolver.host or parsed.port not in (None, 443):
            raise URLError('DoH transport only permits its configured HTTPS host')
        return self.opener.open(request, timeout=timeout)
