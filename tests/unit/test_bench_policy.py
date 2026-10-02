"""The benchmark's approval policy (ADR-022 D3): containment, not a sandbox."""
from __future__ import annotations

import pytest

from personal_ai_core.app.bench.policy import BenchmarkConfirm, ShellRefused, check_shell_command, describe
from personal_ai_core.core.agent import RiskLevel, ToolRequest, ToolSpec


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="", risk_level=RiskLevel.HIGH, input_schema={},
                    timeout_seconds=1, idempotent=False)


def _ask(confirm: BenchmarkConfirm, tool: str, **arguments: str) -> bool:
    return confirm(ToolRequest(tool, arguments), _spec(tool))


@pytest.mark.parametrize("command", [
    "git status",
    "git add -A",
    "git add calc.py",
    'git commit -m "fix: off by one"',
    "git log -n 3 --oneline",
    "git diff HEAD",
    "git mv old.txt new.txt",
    "git rm stale.txt",
    "git restore calc.py",
    "pytest -q",
    "python -m pytest -q tests/test_calc.py",
    "python3 -m pytest",
    "python check.py --verbose",
    "python scripts/check.py",
])
def test_local_git_and_python_on_workspace_files_are_approved(command):
    assert check_shell_command(command, windows=False)
    assert _ask(BenchmarkConfirm(windows=False), "shell", command=command)


@pytest.mark.parametrize("command, reason", [
    ("git push origin main", "outside the benchmark policy"),
    ("git pull", "outside"),
    ("git fetch", "outside"),
    ("git clone https://example.com/x", "outside"),
    ("git remote add o https://example.com/x", "outside"),
    ("git config user.name x", "outside"),
    ("git reset --hard", "outside"),
    ("git checkout main", "outside"),
    ("git -c core.pager=sh log", "no option before"),
    ("git log --output=x.txt", "git option"),
    ("git diff --ext-diff", "git option"),
    ("git commit --template=t.txt", "git option"),
    ("git status --git-dir=x", "git option"),
    ("python -c pass", "nothing else"),
    ("python -m pip install requests", "nothing else"),
    ("python -m http.server", "nothing else"),
    ("python", "nothing else"),
    ("python /etc/x.py", "outside the workspace"),
    ("python ../x.py", "outside the workspace"),
    ("pytest C:/Users/x", "outside the workspace"),
    ("pytest ~/x", "outside the workspace"),
    ("/usr/bin/git status", "by name"),
    ("curl https://example.com", "outside the benchmark policy"),
    ("pip install x", "outside"),
    ("ls", "outside"),
    ("git status && curl x", "metacharacter"),
    ("pytest | tee out", "metacharacter"),
    ("git log > out.txt", "metacharacter"),
    ("echo %PATH%", "metacharacter"),
    ("python $(x).py", "metacharacter"),
    ("pytest -k a*", "metacharacter"),
    ("git status\ncurl x", "metacharacter"),
    ("   ", "empty"),
])
def test_everything_else_is_refused_with_its_reason(command, reason):
    with pytest.raises(ShellRefused, match=reason.split()[0]):
        check_shell_command(command, windows=False)
    confirm = BenchmarkConfirm(windows=False)
    assert not _ask(confirm, "shell", command=command)
    assert confirm.answers[-1].reason.startswith("shell:")


def test_a_colon_in_a_commit_message_is_not_a_drive():
    assert check_shell_command('git commit -m "a: b"', windows=True)


def test_single_quotes_are_refused_on_windows_only():
    assert check_shell_command("git commit -m 'msg'", windows=False)
    with pytest.raises(ShellRefused, match="cmd"):
        check_shell_command("git commit -m 'msg'", windows=True)


def test_network_tools_are_denied_and_recorded():
    confirm = BenchmarkConfirm(windows=False)
    assert not _ask(confirm, "web_search", query="x")
    assert not _ask(confirm, "fetch_url", url="https://example.com")
    assert [a.approved for a in confirm.answers] == [False, False]
    assert all("network" in a.reason for a in confirm.answers)


def test_workspace_tools_validated_by_themselves_are_approved():
    confirm = BenchmarkConfirm(windows=False)
    assert _ask(confirm, "delete_file", path="a.txt")
    assert _ask(confirm, "run_command", command="pytest -q")


def test_an_unknown_tool_is_denied():
    confirm = BenchmarkConfirm(windows=False)
    assert not _ask(confirm, "send_email", to="x")
    assert "not approved" in confirm.answers[0].reason


def test_every_answer_is_kept_in_order_with_its_arguments():
    confirm = BenchmarkConfirm(windows=False)
    _ask(confirm, "shell", command="git status")
    _ask(confirm, "shell", command="git push")
    _ask(confirm, "web_search", query="q")
    assert [(a.tool, a.approved) for a in confirm.answers] == [
        ("shell", True), ("shell", False), ("web_search", False)]
    assert confirm.answers[1].arguments == {"command": "git push"}


def test_the_description_states_the_limit_and_does_not_claim_a_sandbox():
    text = describe()
    assert "Not a security sandbox" in text
    assert "not isolated from the network" in text
    assert "web_search" in text and "fetch_url" in text
