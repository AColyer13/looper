"""Golden tests for looper tool_calls parsing — runs READ-ONLY against /workspace/looper.

Usage:
  PYTHONPATH=/workspace/looper/src /workspace/looper/.venv/bin/python -m pytest \\
    /workspace/looper-fixtures-staging/test_golden_tool_calls.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looper import tools as toolmod

FIX = Path(__file__).resolve().parent / "fixtures" / "fable"
CASES = FIX / "cases.jsonl"
SAMPLES = FIX / "raw_protocol_samples.jsonl"


def load_cases():
    rows = []
    with CASES.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


CASES_DATA = load_cases()


def _tools(case):
    return toolmod.parse_tools(case["tools"])


def _expected_calls(case):
    """Normalize expected via format_tool_call so key order / flattening matches looper."""
    out = []
    for tc in case["tool_calls"]:
        out.append(toolmod.format_tool_call(tc["tool"], tc["args"], call_id=tc["id"]))
    return out


def make_openai_raw(case):
    text = case["text"]
    expected = case["tool_calls"]
    msg = {"role": "assistant", "content": text or None}
    if expected:
        msg["tool_calls"] = [
            {
                "id": e["id"],
                "type": "function",
                "function": {
                    "name": e["tool"],
                    "arguments": json.dumps(e["args"], ensure_ascii=False),
                },
            }
            for e in expected
        ]
    return {
        "choices": [
            {
                "message": msg,
                "finish_reason": "tool_calls" if expected else "stop",
            }
        ]
    }


def make_anthropic_raw(case):
    text = case["text"]
    expected = case["tool_calls"]
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for e in expected:
        content.append(
            {"type": "tool_use", "id": e["id"], "name": e["tool"], "input": e["args"]}
        )
    return {
        "content": content,
        "stop_reason": "tool_use" if expected else "end_turn",
    }


def make_ollama_raw(case):
    text = case["text"]
    expected = case["tool_calls"]
    msg = {"role": "assistant", "content": text or ""}
    if expected:
        msg["tool_calls"] = [
            {
                "id": e["id"],
                "function": {"name": e["tool"], "arguments": e["args"]},
            }
            for e in expected
        ]
    return {"message": msg}


def make_json_block(case):
    text = case["text"]
    expected = case["tool_calls"]
    if not expected:
        return text
    calls = [{"tool": e["tool"], "id": e["id"], **e["args"]} for e in expected]
    block = (
        "```looper-tools\n"
        + json.dumps({"tool_calls": calls}, ensure_ascii=False)
        + "\n```"
    )
    return (text.rstrip() + "\n\n" + block) if text else block


def _assert_calls_equal(got, expected, case_id, protocol):
    assert len(got) == len(expected), (
        f"{case_id}/{protocol}: call count {len(got)} != {len(expected)}"
    )
    for i, (g, e) in enumerate(zip(got, expected)):
        assert g["id"] == e["id"], f"{case_id}/{protocol}[{i}].id {g['id']!r} != {e['id']!r}"
        assert g["tool"] == e["tool"], f"{case_id}/{protocol}[{i}].tool"
        assert g["args"] == e["args"], (
            f"{case_id}/{protocol}[{i}].args mismatch for {e['tool']}:\n"
            f" got keys={sorted(g['args'])} exp keys={sorted(e['args'])}\n"
            f" got={json.dumps(g['args'], ensure_ascii=False)[:300]}\n"
            f" exp={json.dumps(e['args'], ensure_ascii=False)[:300]}"
        )


@pytest.mark.parametrize("case", CASES_DATA, ids=lambda c: c["id"])
def test_openai_chat_extract(case):
    tools = _tools(case)
    raw = make_openai_raw(case)
    text, calls, finish, warnings = toolmod.extract_native_openai_chat(
        raw, tools, max_calls=None
    )
    assert text == case["text"], f"{case['id']}: text mismatch {text!r} vs {case['text']!r}"
    _assert_calls_equal(calls, _expected_calls(case), case["id"], "openai_chat")
    assert finish == case["finish_reason"]
    assert not any("not valid JSON" in w for w in warnings)


@pytest.mark.parametrize("case", CASES_DATA, ids=lambda c: c["id"])
def test_anthropic_extract(case):
    tools = _tools(case)
    raw = make_anthropic_raw(case)
    text, calls, finish, warnings = toolmod.extract_native_anthropic(
        raw, tools, max_calls=None
    )
    assert text == case["text"]
    _assert_calls_equal(calls, _expected_calls(case), case["id"], "anthropic")
    assert finish == case["finish_reason"]


@pytest.mark.parametrize("case", CASES_DATA, ids=lambda c: c["id"])
def test_ollama_extract(case):
    tools = _tools(case)
    raw = make_ollama_raw(case)
    text, calls, finish, warnings = toolmod.extract_native_ollama(
        raw, tools, max_calls=None
    )
    assert text == case["text"]
    _assert_calls_equal(calls, _expected_calls(case), case["id"], "ollama")
    assert finish == case["finish_reason"]


@pytest.mark.parametrize("case", CASES_DATA, ids=lambda c: c["id"])
def test_json_block_parse(case):
    tools = _tools(case)
    raw_text = make_json_block(case)
    text, calls, warnings, finish = toolmod.parse_json_block(raw_text, tools, max_calls=None)
    assert text == case["text"], f"{case['id']}: cleaned text {text!r} != {case['text']!r}"
    _assert_calls_equal(calls, _expected_calls(case), case["id"], "json_block")
    assert not any("malformed" in w for w in warnings)


@pytest.mark.parametrize("case", CASES_DATA, ids=lambda c: c["id"])
def test_parse_tools_and_request_converters(case):
    specs = _tools(case)
    assert len(specs) == len(case["tools"])

    # Outgoing tool schemas
    oa = toolmod.tools_for_openai_chat(specs)
    assert all(t["type"] == "function" for t in oa)
    an = toolmod.tools_for_anthropic(specs)
    assert all("input_schema" in t for t in an)
    ol = toolmod.tools_for_ollama(specs)
    assert ol == oa

    # Message conversion round-paths (assistant with tool_calls)
    msgs = toolmod.normalize_messages(case["messages"])
    if case["tool_results"]:
        msgs = toolmod.apply_tool_results(msgs, case["tool_results"])
        assert any(m["role"] == "tool" for m in msgs)
        # ids preserved
        for tr, m in zip(
            case["tool_results"], [m for m in msgs if m["role"] == "tool"]
        ):
            assert m["tool_call_id"] == tr["id"]

    oa_msgs = toolmod.messages_to_openai_chat(msgs)
    assert oa_msgs
    _sys, an_msgs = toolmod.messages_to_anthropic(msgs)
    assert isinstance(an_msgs, list)
    ol_msgs = toolmod.messages_to_ollama(msgs)
    assert ol_msgs == oa_msgs


def test_raw_protocol_samples_file():
    """Spot-check the fully-materialized multi-protocol sample file against parsers."""
    assert SAMPLES.exists()
    n = 0
    with SAMPLES.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            n += 1
            # Find matching case for tools
            case = next(c for c in CASES_DATA if c["id"] == rec["id"])
            tools = _tools(case)
            exp = _expected_calls(case)

            text, calls, finish, _ = toolmod.extract_native_openai_chat(
                rec["raw"]["openai_chat"], tools, max_calls=None
            )
            assert text == rec["expected"]["text"]
            _assert_calls_equal(calls, exp, rec["id"], "sample/openai")

            text, calls, finish, _ = toolmod.extract_native_anthropic(
                rec["raw"]["anthropic"], tools, max_calls=None
            )
            _assert_calls_equal(calls, exp, rec["id"], "sample/anthropic")

            text, calls, finish, _ = toolmod.extract_native_ollama(
                rec["raw"]["ollama"], tools, max_calls=None
            )
            _assert_calls_equal(calls, exp, rec["id"], "sample/ollama")

            text, calls, _, finish = toolmod.parse_json_block(
                rec["raw"]["json_block"], tools, max_calls=None
            )
            _assert_calls_equal(calls, exp, rec["id"], "sample/json_block")
    assert n >= 10
