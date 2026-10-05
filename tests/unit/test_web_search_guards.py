"""web_search fetches as fetch_url does, and reads no more than the cap.

Gap analysis P2-8. The search used a fetch that followed redirects and had no
public-address check, and parsed the whole page however large it was. It now
uses fetch_url's fetch -- public addresses only, no redirect followed -- and
parses the first MAX_PAGE_BYTES only.
"""
from __future__ import annotations

from personal_ai_core.agent.web import MAX_PAGE_BYTES, WebSearch, urllib_fetch_url

RESULT = (
    '<div class="result"><a class="result__a" href="https://example.org/a">A title</a>'
    '<a class="result__snippet">a snippet</a></div>'
)


def _returning(body: bytes):
    def fetch(url: str, timeout: float):
        return url, "text/html; charset=utf-8", body
    return fetch


def test_the_search_fetches_as_fetch_url_does():
    assert WebSearch()._fetch is urllib_fetch_url


def test_results_within_the_cap_are_read_from_an_oversized_page():
    page = (RESULT.encode() + b" " * (MAX_PAGE_BYTES + 1000))
    result = WebSearch(_returning(page)).run({"query": "anything"})
    assert result.ok and "https://example.org/a" in (result.output or "")


def test_nothing_past_the_cap_is_read():
    page = b" " * MAX_PAGE_BYTES + RESULT.encode()
    result = WebSearch(_returning(page)).run({"query": "anything"})
    assert result.ok and "https://example.org/a" not in (result.output or "")
    assert "no results" in (result.output or "")
