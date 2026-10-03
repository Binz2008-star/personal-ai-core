"""ADR-024's evidence: the 98 protocol errors of #198, split by cause and replayed.

The candidate parsers here are NOT the loop's parser. They are what ADR-024
proposes (unit A), written in the test so the proposal's numbers are pinned to the
recorded text before any of it is built. If unit A is authorized and built, its
own tests replace these.
"""
from __future__ import annotations

import ast
import json
import re
import warnings
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evals" / "results" / "bench"
FILES = (RESULTS / "bench-20261003T161348Z.jsonl", RESULTS / "bench-20261003T164631Z.jsonl")
TOOLS = ("read_file", "list_directory", "search_text", "find_files", "write_file",
         "delete_file", "run_command", "shell", "web_search", "fetch_url")
FUNCTION_CALL = re.compile(r"^\W*(" + "|".join(TOOLS) + r")\s*\(")
# A tool with exactly one required field takes a string `arguments` as that field.
SINGLE_FIELD = {"run_command": "command", "shell": "command", "read_file": "path",
                "delete_file": "path"}


def _protocol_errors():
    for path in FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            run = json.loads(line) if line else {}
            if run.get("kind") != "run" or run.get("track") != "agent":
                continue
            calls = run.get("model_calls") or []
            for refused in run["refused_replies"]:
                if refused["kind"] == "protocol_error":
                    index = refused["call"] - 1
                    done = calls[index].get("done_reason") if 0 <= index < len(calls) else None
                    yield run, refused, done


def _cause(text: str, error: str, done: str | None) -> str:
    """ADR-024 section 1: the first row that matches, in the table's order."""
    if done == "length":
        return "cut at the generation limit"
    if FUNCTION_CALL.match(text.strip()):
        return "function-call syntax"
    if '"answer" must be a string' in error:
        return "numeric answer"
    if '"arguments" must be an object' in error:
        return "command as a string"
    if "not valid JSON" in error and '"write_file"' in text:
        return "write_file content breaks JSON"
    return "rest"


def test_the_98_protocol_errors_split_by_cause_as_the_adr_table_says():
    causes = Counter(_cause(r["text"], r["error"] or "", done) for _, r, done in _protocol_errors())
    assert causes == {"write_file content breaks JSON": 37, "rest": 31, "function-call syntax": 9,
                      "command as a string": 8, "cut at the generation limit": 8,
                      "numeric answer": 5}


def _span(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else None


def _recover(text: str) -> tuple[str, dict] | None:
    """ADR-024 unit A, as proposed: P3 then P1 and P2. Returns (rule, proposal)."""
    span = _span(text)
    if span is None:
        return None
    rules = []
    try:
        value = json.loads(span, strict=False)
    except ValueError:
        try:
            with warnings.catch_warnings():
                # A backslash the reply did not mean as an escape is not news.
                warnings.simplefilter("ignore", DeprecationWarning)
                warnings.simplefilter("ignore", SyntaxWarning)
                value = ast.literal_eval(span)
        except (ValueError, SyntaxError):
            return None
        rules.append("P3")
    if not isinstance(value, dict):
        return None
    answer = value.get("answer")
    if isinstance(answer, (int, float)) and not isinstance(answer, bool):
        value, rules = {"answer": str(answer)}, rules + ["P1"]
    if isinstance(value.get("arguments"), str) and value.get("tool") in SINGLE_FIELD:
        value = {"tool": value["tool"],
                 "arguments": {SINGLE_FIELD[value["tool"]]: value["arguments"]}}
        rules.append("P2")
    if "answer" in value and isinstance(value["answer"], str):
        return "+".join(rules), value
    if value.get("tool") in TOOLS and isinstance(value.get("arguments", {}), dict):
        return "+".join(rules), value
    return None


def test_the_three_candidate_rules_recover_17_of_98_and_nothing_else_parses():
    recovered = Counter()
    for _, refused, _ in _protocol_errors():
        result = _recover(refused["text"])
        if result is not None:
            recovered[result[0]] += 1
    assert recovered == {"P1": 5, "P2": 8, "P3": 4}


def test_one_broken_write_file_call_is_recovered_and_dropping_bad_escapes_recovers_none():
    broken = [r for _, r, done in _protocol_errors()
              if _cause(r["text"], r["error"] or "", done) == "write_file content breaks JSON"]
    assert len(broken) == 37
    recovered = [result for r in broken if (result := _recover(r["text"]))]
    # P3: content given in single quotes, read as the right text.
    assert [(rule, value["arguments"]) for rule, value in recovered] == [
        ("P3", {"path": "config/settings.json", "content": '{"debug": false, "workers": 4}'})]

    def drop_bad_escape(text: str) -> bool:
        span = _span(text)
        try:
            return span is not None and isinstance(
                json.loads(span.replace("\\'", "'"), strict=False), dict)
        except ValueError:
            return False

    assert not any(drop_bad_escape(r["text"]) for r in broken)


def test_every_recovered_tool_call_names_a_real_tool_with_object_arguments():
    for _, refused, _ in _protocol_errors():
        result = _recover(refused["text"])
        if result and "tool" in result[1]:
            assert result[1]["tool"] in TOOLS
            assert isinstance(result[1]["arguments"], dict)
