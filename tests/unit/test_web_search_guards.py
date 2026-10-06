"""web_search fetches as fetch_url does, and reads no more than the cap.

Gap analysis P2-8. The search used a fetch that followed redirects and had no
public-address check: the search URL is fixed, but a redirect from it reached a
destination nobody confirmed -- this machine, the local network, the cloud
metadata address. It now goes through fetch_url's own fetch
(`urllib_fetch_url`): public addresses only, no redirect followed, the same
bytes read (MAX_PAGE_BYTES + 1) and the same timeout argument.

No test touches the network. The transport under urllib's HTTP(S) handlers --
`do_open`, which the guarded handlers and urllib's stock ones both call -- is
replaced by a fake that answers from a script and counts what is read, so an
unguarded fetch would be caught following the redirect too.
"""
from __future__ import annotations

import email.message
import io
import socket
import urllib.request
import urllib.response
from typing import Any

import pytest

from personal_ai_core.agent.web import (
    MAX_PAGE_BYTES,
    SEARCH_URL,
    FetchUrl,
    WebSearch,
    urllib_fetch_url,
)

SECRET_URL = "https://secret.example/stolen"
RESULT = (
    '<div class="result"><a class="result__a" href="https://example.org/a">A title</a>'
    '<a class="result__snippet">a snippet</a></div>'
)
# What a redirect target would hand back: a page that parses as a result, so
# an unguarded fetch that followed the redirect would put it in the output.
TARGET_PAGE = (
    f'<a class="result__a" href="{SECRET_URL}">metadata credentials</a>'
    '<a class="result__snippet">aws_secret_access_key</a>'
).encode()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    # Whatever proxy the machine running the tests has, these tests decide.
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})


class _Counting(io.RawIOBase):
    """An endless body that counts the bytes read from it."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix
        self.read_bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        n = len(buffer)
        offset = self.read_bytes
        chunk = (self._prefix[offset:offset + n] if offset < len(self._prefix) else b"")
        chunk += b" " * (n - len(chunk))
        buffer[:n] = chunk
        self.read_bytes += n
        return n


class FakeTransport:
    """Stands in for the network under every urllib HTTP(S) handler."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], Any]]) -> None:
        self.routes = routes
        self.requested: list[str] = []
        self.timeouts: list[Any] = []

    def __call__(self, handler, http_class, req, **kwargs):
        url = req.full_url
        self.requested.append(url)
        self.timeouts.append(req.timeout)
        key = url.split("?")[0]
        if key not in self.routes:
            raise AssertionError(f"nothing should reach {url}")
        status, header_values, body = self.routes[key]
        headers = email.message.Message()
        for name, value in header_values.items():
            headers[name] = value
        fp: Any = body if isinstance(body, io.IOBase) else io.BytesIO(body)
        response = urllib.response.addinfourl(fp, headers, url, code=status)
        response.msg = "fake"  # type: ignore[attr-defined]
        return response


@pytest.fixture
def transport(monkeypatch):
    def install(routes):
        fake = FakeTransport(routes)

        def do_open(self, http_class, req, **kwargs):
            return fake(self, http_class, req, **kwargs)

        monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", do_open)
        return fake
    return install


def _redirect(location: str):
    return (302, {"Location": location, "Content-Type": "text/html"}, b"")


def _html(body):
    return (200, {"Content-Type": "text/html; charset=utf-8"}, body)


# --- the same fetch as fetch_url -----------------------------------------------------------------


def test_the_search_fetches_as_fetch_url_does():
    assert WebSearch()._fetch is urllib_fetch_url
    assert FetchUrl()._fetch is urllib_fetch_url


def test_a_normal_search_still_works(transport):
    fake = transport({SEARCH_URL: _html(RESULT.encode())})
    result = WebSearch().run({"query": "  what   to find "})
    assert result.ok
    assert "1. A title\n   https://example.org/a\n   a snippet" == result.output
    assert fake.requested == [SEARCH_URL + "?q=what+to+find"]
    assert fake.timeouts == [WebSearch().spec.timeout_seconds]


# --- a redirect from the search endpoint is refused ----------------------------------------------


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:11434/api/tags",
    "http://10.23.45.67/private",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "file:///etc/passwd",
])
def test_a_redirect_from_the_search_endpoint_is_refused(transport, target):
    fake = transport({SEARCH_URL: _redirect(target), target: _html(TARGET_PAGE)})
    result = WebSearch().run({"query": "anything"})

    assert not result.ok
    # Only the search endpoint was asked; the redirect target never was.
    assert fake.requested == [SEARCH_URL + "?q=anything"]
    # A sentence, naming the destination, and nothing from the target.
    error = result.error or ""
    assert error.startswith("search failed: ")
    # (urllib refuses a non-http(s) scheme before the redirect handler sees it;
    # both say why, and name the destination.)
    assert "redirect" in error.lower() and target in error
    assert SECRET_URL not in error and "aws_secret" not in error
    assert result.output is None or SECRET_URL not in result.output


def test_the_search_host_is_refused_when_it_resolves_to_a_private_address(monkeypatch):
    attempted = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))],
    )
    monkeypatch.setattr(socket, "create_connection",
                        lambda address, *a, **k: attempted.append(address))
    result = WebSearch().run({"query": "anything"})
    assert not result.ok
    assert "10.0.0.7, which is not a public address" in (result.error or "")
    assert attempted == []


# --- the same size cap, counted in bytes read ----------------------------------------------------


def test_an_oversized_search_response_is_read_to_the_same_cap_as_fetch_url(transport):
    search_body = _Counting(RESULT.encode())
    page_body = _Counting(b"<html>a page</html>")
    transport({
        SEARCH_URL: _html(search_body),
        "https://example.org/huge": _html(page_body),
    })

    searched = WebSearch().run({"query": "anything"})
    fetched = FetchUrl().run({"url": "https://example.org/huge"})

    # Bounded, and the same bound as fetch_url's.
    assert search_body.read_bytes == page_body.read_bytes == MAX_PAGE_BYTES + 1
    # The results come first on the page, so the search still answers.
    assert searched.ok and "https://example.org/a" in (searched.output or "")
    assert not fetched.ok and f"page over {MAX_PAGE_BYTES} bytes" in (fetched.error or "")


def _returning(body: bytes):
    def fetch(url: str, timeout: float):
        return url, "text/html; charset=utf-8", body
    return fetch


def test_nothing_past_the_cap_is_parsed():
    page = b" " * MAX_PAGE_BYTES + RESULT.encode()
    result = WebSearch(_returning(page)).run({"query": "anything"})
    assert result.ok and "https://example.org/a" not in (result.output or "")
    assert "no results" in (result.output or "")
