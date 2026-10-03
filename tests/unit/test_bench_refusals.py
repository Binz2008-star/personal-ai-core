"""The reading of refused replies (handoff, Next 6b2): its rules, fixed before the data."""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import pytest

from personal_ai_core.agent.loop import parse_reply
from personal_ai_core.app.bench import refusals
from personal_ai_core.app.bench.tasks import load

REPO = Path(__file__).resolve().parents[2]
TASKS = {t.id: t for t in load(REPO / "evals" / "bench")}


def _error(text: str) -> str:
    with pytest.raises(ValueError) as caught:
        parse_reply(text)
    return str(caught.value)


@pytest.mark.parametrize("text, category", [
    ("I will update VERSION and commit the release.", "prose"),
    ("   \n", "empty"),
    ('{"tool": "read_file", "arguments": {"path": "a"}}\n'
     '{"tool": "write_file", "arguments": {"path": "a", "content": "b"}}', "several_objects"),
    ("{'tool': 'read_file', 'arguments': {'path': 'a'}}", "malformed_json"),
    ('{"command": "git commit -m release"}', "wrong_shape"),
    ('{"tool": "read_file", "arguments": ["a"]}', "wrong_shape"),
    ('{"tool": "write_file", "arguments": {"path": "a", "content": "unfinished', "truncated"),
])
def test_each_protocol_error_falls_in_the_first_category_it_matches(text, category):
    assert refusals.classify_protocol_error(text, _error(text)) == category


def test_a_reply_cut_by_the_generation_limit_is_truncated_whatever_it_looks_like():
    text = "I will now"
    assert refusals.classify_protocol_error(text, _error(text), done_reason="length") == "truncated"
    assert refusals.classify_protocol_error(text, _error(text), done_reason="stop") == "prose"


def test_the_tool_names_are_the_tools_the_agent_can_be_offered():
    src = REPO / "src" / "personal_ai_core" / "agent"
    named = set()
    for module in ("tools.py", "web.py"):
        named |= set(re.findall(r'\bname="([a-z_]+)"', (src / module).read_text(encoding="utf-8")))
    assert set(refusals.TOOL_NAMES) == named


def test_a_reply_that_names_a_tool_in_prose_is_marked():
    assert refusals.names_a_tool("I will use write_file to change VERSION.")
    assert not refusals.names_a_tool("The release is ready.")


def test_a_rejected_answer_the_checks_would_accept_is_a_false_rejection(tmp_path):
    task = TASKS["toolsel-count-json"]
    assert refusals.would_have_passed(task, "There are 4 .json files.", tmp_path)
    assert not refusals.would_have_passed(task, "There are 3 .json files.", tmp_path)


def test_a_task_that_needs_a_change_cannot_produce_a_false_rejection(tmp_path):
    for task_id in ("file-create-settings", "git-commit-release", "verify-off-by-one"):
        reference = TASKS[task_id].reference.get("answer") or {"en": "Done."}
        assert not refusals.would_have_passed(TASKS[task_id], reference["en"], tmp_path), task_id


def _run(task: str, language: str, refused: list[dict], calls: int = 3) -> dict:
    return {"kind": "run", "track": "agent", "task": task, "language": language, "run": 1,
            "refused_replies": refused,
            "model_calls": [{"done_reason": "stop"} for _ in range(calls)]}


def test_read_counts_categories_first_replies_and_false_rejections(tmp_path):
    lines = [
        {"kind": "header"},
        _run("toolsel-count-json", "en", [
            {"call": 1, "kind": "protocol_error", "error": _error("I will use find_files."),
             "text": "I will use find_files."},
            {"call": 2, "kind": "action_required", "error": None,
             "text": json.dumps({"answer": "There are 4 .json files."})},
        ]),
        _run("file-create-settings", "ar", [
            {"call": 1, "kind": "action_required", "error": None,
             "text": json.dumps({"answer": "تم."})},
            {"call": 2, "kind": "protocol_error", "error": "x",
             "text": "[reply withheld: it contained something secret-shaped (GitHub token)]"},
        ]),
        {"kind": "run", "track": "agent", "task": "toolsel-count-json", "language": "en", "run": 2},
        {"kind": "run", "track": "knowledge", "task": "kb", "language": "en", "run": 1},
    ]
    reading = refusals.read(lines, TASKS, tmp_path)
    assert (reading.runs_with_field, reading.runs_without_field) == (2, 1)
    assert reading.protocol == {("en", "prose"): 1}
    assert reading.first_reply == {("en", "prose"): 1}
    assert reading.names_tool[("en", "prose")] == 1
    assert reading.rejected == {("en", "toolsel-count-json"): 1, ("ar", "file-create-settings"): 1}
    assert reading.false_rejections == {("en", "toolsel-count-json"): 1}
    assert reading.withheld == 1


def test_the_command_reads_a_file_and_leaves_it_as_it_was(tmp_path):
    path = tmp_path / "bench-x.jsonl"
    lines = [{"kind": "header"}, _run("toolsel-count-json", "en", [
        {"call": 1, "kind": "action_required", "error": None,
         "text": json.dumps({"answer": "4 files."})}])]
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
                    encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    out = io.StringIO()
    assert refusals.main([str(path)], stdout=out) == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    text = out.getvalue()
    assert "toolsel-count-json" in text and "decides nothing" in text
    assert re.search(r"en\s+all\s+1 \|\s+1", text)


def test_a_file_from_before_the_field_says_so(tmp_path):
    old = REPO / "evals" / "results" / "bench" / "bench-20261003T135502Z.jsonl"
    out = io.StringIO()
    refusals.main([str(old)], stdout=out)
    assert "120 agent run(s) record no refused-reply text" in out.getvalue()


def test_a_clipped_rejected_reply_is_judged_as_recorded_not_dropped(tmp_path):
    clipped = '{"answer": "There are 4 .json files' + "x" * 10 + '... [900 more chars]'
    lines = [_run("toolsel-count-json", "en", [
        {"call": 1, "kind": "action_required", "error": None, "text": clipped}])]
    reading = refusals.read(lines, TASKS, tmp_path)
    assert reading.rejected == {("en", "toolsel-count-json"): 1}
    assert reading.false_rejections == {("en", "toolsel-count-json"): 1}


@pytest.mark.parametrize("reply", [
    '{"answer": "There are 4 .json files."}',
    'Sure:\n```json\n{"answer": "line one\nline two"}\n```',
    '{"answer": "done", "note": "extra"}',
])
def test_the_answer_is_read_from_a_rejected_reply_as_the_loop_reads_it(reply):
    assert refusals._answer_text(reply) == parse_reply(reply)["answer"]
