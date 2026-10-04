"""fetch_url reads public addresses only.

The owner confirms a URL, not where its name leads. Before this, a confirmed
`http://127.0.0.1:11434/` reached the machine's own services, and
`http://169.254.169.254/` a cloud machine's credentials; a harmless-looking
name that resolves to either did the same. The default fetch now resolves the
host once, refuses it when any address is not public, and connects to an
address it checked.

No test touches the network: names are resolved by an injected resolver, and a
connection to a public address is stopped before it leaves the machine.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from personal_ai_core.agent.web import FetchUrl, is_public_address, urllib_fetch_url

PUBLIC_V4 = "93.184.215.14"


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    # Whatever proxy the machine running the tests has, these tests decide.
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})


@pytest.fixture
def connections(monkeypatch):
    """Every TCP connection attempted. One to a public address is stopped here."""
    attempted: list[tuple[str, int]] = []
    real = socket.create_connection

    def track(address, *args, **kwargs):
        attempted.append((address[0], address[1]))
        if is_public_address(ipaddress.ip_address(address[0])):
            raise OSError("stopped before leaving the machine")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", track)
    return attempted


def _resolver(*addresses: str, calls: list[str] | None = None):
    def resolve(host: str, port: int) -> list[tuple[Any, ...]]:
        if calls is not None:
            calls.append(host)
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
             (a, port, 0, 0) if ":" in a else (a, port))
            for a in addresses
        ]
    return resolve


def _fetch(resolve, allowed: Callable[[Any], bool] = is_public_address):
    return lambda url, timeout: urllib_fetch_url(url, timeout, resolve=resolve, allowed=allowed)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stop(server):
    server.shutdown()
    server.server_close()


def _recording_handler(hits: list[tuple[str, str]]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("Host", "")))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"the page")

        def log_message(self, format, *args):
            pass

    return Handler


# --- which addresses are public ---------------------------------------------------------------


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.8.9.10", "0.0.0.0",                      # this machine
    "10.1.2.3", "172.16.0.1", "192.168.1.1",                   # RFC 1918
    "169.254.169.254",                                         # link-local: cloud metadata
    "100.64.0.1", "224.0.0.1", "255.255.255.255",              # shared, multicast, broadcast
    "::1", "::", "fe80::1", "fd00::1", "fc00::1", "ff02::1",   # IPv6 equivalents
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",  # IPv4-mapped
    "::127.0.0.1",                                             # IPv4-compatible (deprecated)
    "2002:7f00:1::", "2002:a9fe:a9fe::",                       # 6to4 around 127.0.0.1 / metadata
    "64:ff9b::a00:1",                                          # NAT64 around 10.0.0.1
])
def test_these_addresses_are_not_public(address):
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [
    PUBLIC_V4, "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808",
])
def test_these_addresses_are_public(address):
    assert is_public_address(ipaddress.ip_address(address))


# --- the default fetch refuses them, before connecting ------------------------------------------


@pytest.mark.parametrize("host", ["localhost", "2130706433", "127.1", "0x7f000001"])
def test_other_spellings_of_this_machine_are_not_reached(host):
    # Names and the numeric forms getaddrinfo reads as 127.0.0.1. Where a
    # platform does not resolve a form at all the fetch fails anyway; either
    # way the server is not reached.
    hits: list[tuple[str, str]] = []
    server = _serve(_recording_handler(hits))
    try:
        result = FetchUrl().run({"url": f"http://{host}:{server.server_port}/page"})
    finally:
        _stop(server)
    assert not result.ok
    assert hits == []


def test_ipv6_loopback_is_refused_before_a_connection(connections):
    result = FetchUrl().run({"url": "http://[::1]:8080/page"})
    assert not result.ok and "not a public address" in (result.error or "")
    assert connections == []


@pytest.mark.parametrize("address", [
    "10.0.0.5", "192.168.1.10", "169.254.169.254", "127.0.0.1",
    "fd12:3456::1", "fe80::1%eth0", "::ffff:127.0.0.1",
])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_name_that_resolves_to_a_private_address_is_refused(connections, address, scheme):
    calls: list[str] = []
    result = FetchUrl(_fetch(_resolver(address, calls=calls))).run(
        {"url": f"{scheme}://notes.example/today"})
    assert not result.ok
    assert f"notes.example is {address.split('%')[0]}" in (result.error or "")
    assert connections == []
    assert calls == ["notes.example"]


def test_one_private_address_among_public_ones_refuses_the_name(connections):
    result = FetchUrl(_fetch(_resolver(PUBLIC_V4, "10.0.0.5"))).run(
        {"url": "http://mixed.example/"})
    assert not result.ok and "10.0.0.5" in (result.error or "")
    # Refused outright, not tried address by address.
    assert connections == []


def test_the_connection_goes_to_the_checked_address_not_a_second_lookup(connections):
    # DNS rebinding: a name that is public when checked and private when
    # connected to. The name is looked up once and the connection goes to the
    # address that was checked.
    calls: list[str] = []

    def rebinding(host: str, port: int) -> list[tuple[Any, ...]]:
        calls.append(host)
        address = PUBLIC_V4 if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    for scheme, port in (("http", 80), ("https", 443)):
        calls.clear()
        connections.clear()
        result = FetchUrl(_fetch(rebinding)).run({"url": f"{scheme}://rebind.example/"})
        assert not result.ok and "stopped before leaving the machine" in (result.error or "")
        assert connections == [(PUBLIC_V4, port)]
        assert calls == ["rebind.example"]


def test_a_public_name_is_fetched_by_name_at_the_checked_address():
    # The local server stands in for a public host: the resolver names it,
    # and `allowed` accepts exactly its address. The request carries the
    # host's name; the connection goes to the address the guard returned.
    hits: list[tuple[str, str]] = []
    server = _serve(_recording_handler(hits))
    port = server.server_port
    try:
        result = FetchUrl(_fetch(_resolver("127.0.0.1"),
                                 allowed=lambda a: str(a) == "127.0.0.1")).run(
            {"url": f"http://page.example:{port}/article"})
    finally:
        _stop(server)
    assert result.ok and (result.output or "").endswith("the page")
    assert hits == [("/article", f"page.example:{port}")]


# --- through a proxy, the name is checked before the proxy is asked ------------------------------


@pytest.fixture
def proxy(monkeypatch):
    hits: list[tuple[str, str]] = []
    server = _serve(_recording_handler(hits))
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"http": f"http://127.0.0.1:{server.server_port}"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    yield hits
    _stop(server)


def test_through_a_proxy_a_private_name_is_refused_before_the_proxy_sees_it(proxy):
    result = FetchUrl(_fetch(_resolver("10.0.0.5"))).run({"url": "http://internal.example/admin"})
    assert not result.ok and "internal.example is 10.0.0.5" in (result.error or "")
    assert proxy == []


def test_through_a_proxy_a_loopback_literal_is_refused(proxy):
    result = FetchUrl().run({"url": "http://127.0.0.1:11434/api/tags"})
    assert not result.ok and "not a public address" in (result.error or "")
    assert proxy == []


def test_through_a_proxy_a_public_name_is_handed_to_the_proxy(proxy):
    result = FetchUrl(_fetch(_resolver(PUBLIC_V4))).run({"url": "http://public.example/page"})
    assert result.ok
    assert proxy == [("http://public.example/page", "public.example")]


def test_through_a_proxy_a_name_that_does_not_resolve_here_is_the_proxys_to_resolve(proxy):
    # Behind a proxy, the proxy is often the only route to DNS.
    def unresolvable(host: str, port: int) -> list[tuple[Any, ...]]:
        raise socket.gaierror(socket.EAI_NONAME, "not known here")

    result = FetchUrl(_fetch(unresolvable)).run({"url": "http://only-the-proxy-knows.example/"})
    assert result.ok
    assert proxy == [("http://only-the-proxy-knows.example/", "only-the-proxy-knows.example")]
