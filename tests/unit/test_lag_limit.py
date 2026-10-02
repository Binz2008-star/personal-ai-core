"""The limit the lag gates apply (tests/support/lag_limit.py): one lower on a pull request."""
from __future__ import annotations

import pytest
from support.lag_limit import PULL_REQUEST, effective_limit


@pytest.mark.parametrize("event, expected", [
    (PULL_REQUEST, 2), ("push", 3), ("workflow_dispatch", 3), ("schedule", 3), ("", 3),
])
def test_only_a_pull_request_run_is_held_one_below(event, expected):
    assert effective_limit(3, {"GITHUB_EVENT_NAME": event}) == expected


def test_a_local_run_has_no_event_and_gets_the_full_limit():
    assert effective_limit(3, {}) == 3


def test_it_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    assert effective_limit(3) == 2
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert effective_limit(3) == 3
    monkeypatch.delenv("GITHUB_EVENT_NAME")
    assert effective_limit(3) == 3


@pytest.mark.parametrize("maximum", [1, 2, 3, 10])
def test_it_is_always_exactly_one_below_on_a_pull_request(maximum):
    assert effective_limit(maximum, {"GITHUB_EVENT_NAME": PULL_REQUEST}) == maximum - 1
