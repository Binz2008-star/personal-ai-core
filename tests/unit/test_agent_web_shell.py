"""The agent's reach: web_search, fetch_url and shell.

No test touches the network: the web tools take an injected `fetch`.
"""
from __future__ import annotations

import sys

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import Shell, shell_environment
from personal_ai_core.agent.web import (
    MAX_PAGE_BYTES,
    MAX_TEXT_CHARS,
    FetchUrl,
    WebSearch,
    html_to_text,
    parse_results,
)
from personal_ai_core.core.agent import Decision, RiskLevel, ToolRequest

RESULTS = """
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.am.gov.ae%2Fen%2Fgrease&amp;rut=abc">
       Grease trap <b>regulations</b> &amp; permits</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Food establishments in
     <b>Ajman</b> must install approved grease traps.</a>
</div>
<div class="result">
  <a class="result__a" href="https://example.org/direct">Direct link</a>
  <a class="result__snippet">second snippet</a>
</div>
<div class="result">
  <a class="result__a" href="javascript:alert(1)">not a web result</a>
</div>
"""


def fetch_returning(body, content_type="text/html; charset=utf-8", final=None):
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return final or url, content_type, body.encode("utf-8") if isinstance(body, str) else body

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def failing(url, timeout):
    raise OSError("Tunnel connection failed: 403 Forbidden")


# --- web_search ---------------------------------------------------------------------


def test_results_are_parsed_and_redirects_unwrapped():
    results = parse_results(RESULTS)
    assert results == [
        {
            "title": "Grease trap regulations & permits",
            "url": "https://www.am.gov.ae/en/grease",
            "snippet": "Food establishments in Ajman must install approved grease traps.",
        },
        {"title": "Direct link", "url": "https://example.org/direct", "snippet": "second snippet"},
    ]


def test_web_search_returns_numbered_results():
    fetch = fetch_returning(RESULTS)
    result = WebSearch(fetch).run({"query": "  grease   trap Ajman "})
    assert result.ok
    assert result.output.startswith("1. Grease trap regulations & permits\n   https://www.am.gov.ae/en/grease")
    assert "2. Direct link" in result.output
    assert fetch.calls == ["https://html.duckduckgo.com/html/?q=grease+trap+Ajman"]  # type: ignore[attr-defined]


def test_web_search_says_when_nothing_came_back():
    result = WebSearch(fetch_returning("<html>anomaly</html>")).run({"query": "x"})
    assert result.ok and "no results" in result.output


def test_web_search_reports_a_network_failure():
    result = WebSearch(failing).run({"query": "x"})
    assert not result.ok and "403 Forbidden" in (result.error or "")


def test_web_search_needs_a_query():
    assert not WebSearch(fetch_returning("")).run({"query": "   "}).ok


# --- fetch_url ------------------------------------------------------------------------


PAGE = """<html><head><title> Grease Trap Guide </title><style>p{color:red}</style>
<script>steal()</script></head><body><h1>Guide</h1><p>Clean   every
month.</p><ul><li>one</li><li>two</li></ul></body></html>"""


def test_html_becomes_readable_text():
    title, text = html_to_text(PAGE)
    assert title == "Grease Trap Guide"
    assert text == "Guide\nClean every month.\none\ntwo"
    assert "steal" not in text and "color" not in text


def test_fetch_url_reads_a_page():
    result = FetchUrl(fetch_returning(PAGE, final="https://x.test/guide")).run(
        {"url": "https://x.test/g"}
    )
    assert result.ok
    assert result.output.startswith("Grease Trap Guide\nhttps://x.test/guide\n\nGuide")


def test_fetch_url_reads_plain_text_and_json():
    result = FetchUrl(fetch_returning('{"a": 1}', "application/json")).run({"url": "https://x.test"})
    assert result.ok and result.output.endswith('{"a": 1}')


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.test/a", "javascript:x", "notes.md"])
def test_fetch_url_only_speaks_http(url):
    fetch = fetch_returning(PAGE)
    result = FetchUrl(fetch).run({"url": url})
    assert not result.ok and "only http" in (result.error or "")
    assert fetch.calls == []  # type: ignore[attr-defined]


def test_fetch_url_refuses_binary_and_oversized_pages():
    assert "not a text page" in (
        FetchUrl(fetch_returning(b"\x89PNG", "image/png")).run({"url": "https://x.test"}).error or ""
    )
    big = FetchUrl(fetch_returning(b"a" * (MAX_PAGE_BYTES + 1), "text/plain"))
    assert "over" in (big.run({"url": "https://x.test"}).error or "")


def test_a_long_page_is_bounded_and_says_so():
    result = FetchUrl(fetch_returning("a" * (MAX_TEXT_CHARS * 2), "text/plain")).run(
        {"url": "https://x.test"}
    )
    assert result.ok and result.truncated and len(result.output) == MAX_TEXT_CHARS


def test_fetch_url_reports_a_network_failure():
    result = FetchUrl(failing).run({"url": "https://x.test"})
    assert not result.ok and "could not fetch" in (result.error or "")


# --- the policy: what runs without asking --------------------------------------------


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


def test_search_runs_without_asking_fetch_and_shell_ask(ws):
    policy = RiskPolicy()
    search, fetch, shell = WebSearch(failing), FetchUrl(failing), Shell(ws)
    assert search.spec.risk_level is RiskLevel.MEDIUM
    assert policy.decide(ToolRequest("web_search", {}), search.spec).decision is Decision.ALLOW
    assert policy.decide(ToolRequest("fetch_url", {}), fetch.spec).decision is Decision.ASK
    assert policy.decide(ToolRequest("shell", {}), shell.spec).decision is Decision.ASK


def test_a_url_is_not_fetched_unless_the_user_says_yes(ws):
    fetch = fetch_returning(PAGE)
    executor = ToolExecutor([FetchUrl(fetch)], RiskPolicy(), confirm=lambda r, s: False)
    record = executor.execute(ToolRequest("fetch_url", {"url": "https://x.test/?q=secret"}))
    assert not record.executed and fetch.calls == []  # type: ignore[attr-defined]


# --- shell ------------------------------------------------------------------------------


def run_shell(ws, command):
    executor = ToolExecutor([Shell(ws)], RiskPolicy(), confirm=lambda r, s: True)
    record = executor.execute(ToolRequest("shell", {"command": command}))
    assert record.result is not None
    return record


def test_shell_runs_a_pipeline_in_the_workspace(ws):
    (ws.root / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    python = f'"{sys.executable}"'
    record = run_shell(ws, f"{python} -c \"print(open('a.txt').read().upper())\" | {python} -c \"import sys; print(sys.stdin.read().strip()[::-1])\"")
    assert record.executed and record.result.ok  # type: ignore[union-attr]
    assert record.result.output.strip() == "ATEB\nAHPLA"  # type: ignore[union-attr]


def test_shell_reports_a_failing_command(ws):
    record = run_shell(ws, f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    assert not record.result.ok and record.result.error == "exit code 3"  # type: ignore[union-attr]


def test_shell_is_not_run_without_a_yes(ws):
    executor = ToolExecutor([Shell(ws)], RiskPolicy(), confirm=lambda r, s: False)
    record = executor.execute(ToolRequest("shell", {"command": "echo hi > made.txt"}))
    assert not record.executed and not (ws.root / "made.txt").exists()


def test_the_shell_does_not_see_secret_variables(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("PAC_WORKSPACE_NOTE", "kept")
    env = shell_environment()
    assert "GITHUB_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert "DATABASE_URL" not in env and env["PAC_WORKSPACE_NOTE"] == "kept"
    assert "PATH" in env


# --- wired into the agent -----------------------------------------------------------------


def test_the_agent_has_the_new_tools(tmp_path):
    from personal_ai_core.conversation.factory import build_agent

    agent = build_agent(workspace=tmp_path, web_fetch=failing)
    assert {"shell", "web_search", "fetch_url", "read_file", "run_command"} <= set(agent.executor.specs)


def test_preformatted_text_keeps_its_lines():
    _, text = html_to_text("<p>a\nb</p><pre>line 1\nline 2</pre>")
    assert text == "a b\nline 1\nline 2"
