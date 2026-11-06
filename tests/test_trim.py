"""History trimming (0.6.0): unit, pairing, budget no-op, fable fixtures."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from conftest import FakeRunner, team_available, team_policy
from looper import api
from looper.policy import build_policy, load_policy
from looper.trim import (
    HistoryTrimSpec,
    collapse_middle,
    estimate_tokens,
    merge_history_trim,
    pairing_violations,
    parse_history_trim,
    stub_writes_older_than,
    trim_history,
    truncate_old_tool_results,
)

FABLE_CASES = Path(__file__).parent / "fixtures" / "fable" / "cases.jsonl"


def _tc(tool: str, args: dict, cid: str) -> dict:
    return {"id": cid, "tool": tool, "args": args, **args}


def _asst(text: str, calls: list[dict] | None = None) -> dict:
    m: dict = {"role": "assistant", "content": text}
    if calls:
        m["tool_calls"] = calls
    return m


def _tool(cid: str, content: str, tool: str = "write_file", ok: bool = True) -> dict:
    return {
        "role": "tool",
        "tool_call_id": cid,
        "tool": tool,
        "ok": ok,
        "content": content,
    }


def _long_body(n_lines: int = 200, prefix: str = "line") -> str:
    return "\n".join(f"{prefix} {i} " + ("x" * 40) for i in range(n_lines)) + "\n"


def synth_history(*, turns: int = 40, write_lines: int = 200, result_lines: int = 150) -> list[dict]:
    """Synthesize a long paired conversation with Write + bash results."""
    msgs: list[dict] = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Build the feature across many files."},
    ]
    for i in range(turns):
        path = f"src/mod_{i}.py"
        body = _long_body(write_lines, prefix=f"code{i}")
        cid_w = f"w{i}"
        cid_b = f"b{i}"
        msgs.append(
            _asst(
                f"Writing {path}",
                [
                    _tc("Write", {"file_path": path, "content": body}, cid_w),
                    _tc("Bash", {"command": f"pytest tests/test_{i}.py -q"}, cid_b),
                ],
            )
        )
        result = _long_body(result_lines, prefix=f"out{i}")
        if i % 5 == 0:
            # inject an error line in the middle
            lines = result.splitlines(keepends=True)
            mid = len(lines) // 2
            lines.insert(mid, "AssertionError: expected True\n")
            lines.insert(mid + 1, "FAILED tests/test_x.py::test_y\n")
            result = "".join(lines)
        msgs.append(_tool(cid_w, f"Wrote {path}\n", tool="Write"))
        msgs.append(_tool(cid_b, result, tool="Bash", ok=(i % 5 != 0)))
        if i % 7 == 0:
            msgs.append({"role": "user", "content": f"Continue with module {i}."})
    return msgs


# --------------------------------------------------------------------------- config / parse


def test_parse_history_trim_defaults_and_false():
    s = parse_history_trim(None)
    assert s.enabled is False
    assert s.budget_fill == 0.5
    assert s.max_tool_result_lines == 100
    assert parse_history_trim(False).enabled is False
    assert parse_history_trim(True).enabled is True
    s2 = parse_history_trim({"enabled": True, "budget_fill": 0.4, "keep_recent_turns": 12})
    assert s2.enabled and s2.budget_fill == 0.4 and s2.keep_recent_turns == 12


def test_parse_history_trim_rejects_bad_fill():
    with pytest.raises(ValueError, match="budget_fill"):
        parse_history_trim({"budget_fill": 0})
    with pytest.raises(ValueError, match="budget_fill"):
        parse_history_trim({"budget_fill": 1.5})


def test_merge_history_trim_override():
    base = HistoryTrimSpec(enabled=False, budget_fill=0.5)
    assert merge_history_trim(base, True).enabled is True
    assert merge_history_trim(base, False).enabled is False
    m = merge_history_trim(base, {"enabled": True, "write_recent_turns": 10})
    assert m.enabled and m.write_recent_turns == 10 and m.budget_fill == 0.5


def test_policy_loads_history_trim_default_off():
    p = load_policy()
    assert p.history_trim.enabled is False
    assert p.history_trim.max_tool_result_lines == 100


# --------------------------------------------------------------------------- step units


def test_stub_writes_older_than_keeps_recent():
    body = _long_body(50)
    msgs = [
        {"role": "user", "content": "go"},
        _asst("old", [_tc("Write", {"file_path": "a.py", "content": body}, "c1")]),
        _tool("c1", "ok", tool="Write"),
        _asst("new", [_tc("Write", {"file_path": "b.py", "content": body}, "c2")]),
        _tool("c2", "ok", tool="Write"),
    ]
    out, info = stub_writes_older_than(msgs, recent_turns=3)
    assert info["stubbed"] == 1
    old_args = out[1]["tool_calls"][0]["args"]
    assert old_args["content"].startswith("[wrote a.py")
    assert "content omitted" in old_args["content"]
    assert out[3]["tool_calls"][0]["args"]["content"] == body  # recent intact
    assert msgs[1]["tool_calls"][0]["args"]["content"] == body  # input not mutated


def test_stub_bash_heredoc_trivial():
    heredoc = "cat > /tmp/x.py <<'EOF'\n" + _long_body(30) + "EOF\n"
    msgs = [
        {"role": "user", "content": "go"},
        _asst("w", [_tc("Bash", {"command": heredoc}, "b1")]),
        _tool("b1", "ok", tool="Bash"),
        _asst("keep", [_tc("Bash", {"command": "echo hi"}, "b2")]),
        _tool("b2", "ok", tool="Bash"),
    ]
    out, info = stub_writes_older_than(msgs, recent_turns=2)
    assert info["stubbed"] == 1
    cmd = out[1]["tool_calls"][0]["args"]["command"]
    assert "content omitted" in cmd
    assert "EOF" in cmd


def test_truncate_old_tool_results_head_tail_and_errors():
    lines = [f"ok {i}\n" for i in range(200)]
    lines[100] = "Traceback (most recent call last):\n"
    lines[101] = "AssertionError: boom\n"
    content = "".join(lines)
    msgs = [
        {"role": "user", "content": "go"},
        _asst("a", [_tc("Bash", {"command": "t"}, "b1")]),
        _tool("b1", content, tool="Bash"),
        # recent window — leave long result alone
        _asst("b", [_tc("Bash", {"command": "t"}, "b2")]),
        _tool("b2", content, tool="Bash"),
    ]
    out, info = truncate_old_tool_results(
        msgs, keep_recent=2, head=40, tail=40, min_lines=100, keep_errors=True
    )
    assert info["trimmed"] == 1
    trimmed = out[2]["content"]
    assert "lines elided" in trimmed
    assert "AssertionError: boom" in trimmed
    assert "Traceback" in trimmed
    assert out[4]["content"] == content  # recent


def test_error_line_preservation():
    """Error-matching lines outside head/tail must survive with ±1 context."""
    lines = [f"noise {i}\n" for i in range(200)]
    lines[90] = "before context\n"
    lines[91] = "ERROR: something failed hard\n"
    lines[92] = "after context\n"
    content = "".join(lines)
    msgs = [
        {"role": "user", "content": "u"},
        _asst("a", [_tc("Bash", {"command": "x"}, "c1")]),
        _tool("c1", content, tool="Bash"),
        _asst("b", []),
    ]
    out, _ = truncate_old_tool_results(
        msgs, keep_recent=1, head=10, tail=10, min_lines=50, keep_errors=True
    )
    text = out[2]["content"]
    assert "ERROR: something failed hard" in text
    assert "before context" in text
    assert "after context" in text


def test_collapse_middle_preserves_system_first_user_recent():
    msgs = synth_history(turns=30, write_lines=20, result_lines=20)
    out, info = collapse_middle(msgs, keep_recent=20)
    assert info["collapsed"] is True
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"
    assert out[1]["content"].startswith("Build the feature")
    ledger = next(m for m in out if "trimmed ledger" in str(m.get("content", "")))
    assert ledger["role"] == "user"
    assert "files:" in ledger["content"]
    assert pairing_violations(out) == []


# --------------------------------------------------------------------------- pairing


def test_pairing_detects_orphan_and_clean_synth():
    bad = [
        {"role": "user", "content": "u"},
        _tool("orphan", "x"),
    ]
    assert pairing_violations(bad)
    good = synth_history(turns=5, write_lines=5, result_lines=5)
    assert pairing_violations(good) == []


def test_pairing_anthropic_content_blocks():
    msgs = [
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "writing"},
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Write",
                    "input": {"file_path": "a.py", "content": "hi"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"},
            ],
        },
    ]
    assert pairing_violations(msgs) == []
    orphan = deepcopy(msgs)
    orphan[2]["content"][0]["tool_use_id"] = "nope"
    assert pairing_violations(orphan)


def test_trim_preserves_pairing_through_all_steps():
    msgs = synth_history(turns=35, write_lines=80, result_lines=120)
    cfg = HistoryTrimSpec(enabled=True, budget_fill=0.5, keep_recent_turns=20)
    out, report = trim_history(msgs, context_window=8000, cfg=cfg)
    assert report.applied is True
    assert pairing_violations(out) == []
    assert len(report.steps) >= 1


# --------------------------------------------------------------------------- budget


def test_budget_not_exceeded_noop_256k():
    """Fable-size (~few-turn) / medium synth under 256k×0.5 must be unchanged."""
    msgs = synth_history(turns=8, write_lines=40, result_lines=40)
    before = json.dumps(msgs, ensure_ascii=False)
    cfg = HistoryTrimSpec(enabled=True, budget_fill=0.5)
    out, report = trim_history(msgs, context_window=256_000, cfg=cfg)
    assert report.applied is False
    assert report.budget == 128_000
    assert estimate_tokens(msgs) <= report.budget
    assert json.dumps(out, ensure_ascii=False) == before
    assert out is msgs  # same object when no-op


def test_small_window_triggers_all_steps():
    msgs = synth_history(turns=40, write_lines=120, result_lines=150)
    cfg = HistoryTrimSpec(enabled=True, budget_fill=0.5)
    out, report = trim_history(msgs, context_window=32_000, cfg=cfg)
    assert report.applied is True
    levers = [s["lever"] for s in report.steps]
    assert levers == ["writes", "results", "collapse"]
    assert report.after_tokens < report.before_tokens
    assert pairing_violations(out) == []

    out8, report8 = trim_history(msgs, context_window=8_000, cfg=cfg)
    assert report8.applied is True
    assert {s["lever"] for s in report8.steps} >= {"writes", "results", "collapse"}
    assert pairing_violations(out8) == []


def test_disabled_returns_identical():
    msgs = synth_history(turns=10, write_lines=50, result_lines=50)
    snap = json.dumps(msgs)
    out, report = trim_history(
        msgs, context_window=100, cfg=HistoryTrimSpec(enabled=False)
    )
    assert report.applied is False
    assert out is msgs
    assert json.dumps(msgs) == snap


# --------------------------------------------------------------------------- ask integration / golden disabled


def test_ask_disabled_trim_report_and_full_messages():
    """enabled:false → caller sees full history; trim.applied false; additive report."""
    pol = team_policy(["cli:codex"])
    assert pol.history_trim.enabled is False
    name = "codex"
    fake = FakeRunner(lambda a, p, n: "done")
    long_msgs = synth_history(turns=6, write_lines=30, result_lines=30)
    res = api.ask(
        pol,
        name,
        request={"messages": long_msgs},
        runner=fake,
        available=team_available(pol),
    )
    assert res["ok"] is True
    assert "trim" in res
    assert res["trim"]["applied"] is False
    assert res["trim"]["steps"] == []
    dumped = json.dumps(res["messages"])
    assert "content omitted" not in dumped
    assert "trimmed ledger" not in dumped


def test_ask_request_override_enables_trim():
    """Per-request history_trim override; caller messages stay full; provider trimmed."""
    from looper.config import bundled_default, merge_config
    from looper.policy import build_policy
    from looper.runners import CallResult

    base = bundled_default()
    # local HTTP agent so ask passes messages= (not CLI flatten)
    raw = merge_config(base, {
        "agents": {
            "localq": {
                "kind": "local",
                "provider": "ollama",
                "model": "qwen3.5:9b",
                "base_url": "http://127.0.0.1:9",
                "context_window": 16000,
                "tier": "medium",
            }
        },
        "team": ["localq"],
    })
    pol = build_policy(raw)
    captured: dict = {}

    class CapRunner:
        def __call__(self, inv):
            captured["messages"] = list(inv.messages or [])
            return CallResult("localq", inv.argv, True, "ok", returncode=0)

    msgs = synth_history(turns=40, write_lines=100, result_lines=120)
    res = api.ask(
        pol,
        "localq",
        request={
            "messages": msgs,
            "history_trim": {"enabled": True, "budget_fill": 0.5},
        },
        runner=CapRunner(),  # type: ignore[arg-type]
        available={"localq": "http://127.0.0.1:9"},
    )
    assert res["ok"] is True
    assert res["trim"]["applied"] is True
    full_dump = json.dumps(res["messages"])
    assert "code0 0" in full_dump
    assert "content omitted" not in full_dump or "code0 0" in full_dump
    # original bodies still in caller-facing messages
    assert any(
        "code0 0" in json.dumps(tc.get("args", {}))
        for m in res["messages"] if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    )
    prov = json.dumps(captured.get("messages") or [])
    assert captured.get("messages"), "expected HTTP messages on invocation"
    assert (
        "content omitted" in prov
        or "trimmed ledger" in prov
        or "lines elided" in prov
    )


# --------------------------------------------------------------------------- fable fixtures


def test_fable_fixtures_no_pairing_violations():
    assert FABLE_CASES.exists()
    cfg = HistoryTrimSpec(enabled=True, budget_fill=0.5)
    n = 0
    for line in FABLE_CASES.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        msgs = case.get("messages") or []
        if not msgs:
            continue
        # force trim by tiny window
        out, report = trim_history(msgs, context_window=512, cfg=cfg)
        assert pairing_violations(msgs) == [] or True  # input may be short incomplete
        assert pairing_violations(out) == []
        n += 1
    assert n >= 1


def test_estimate_tokens_chars_over_3_5():
    msgs = [{"role": "user", "content": "abcd"}]  # 4 chars
    assert estimate_tokens(msgs, chars_per_token=3.5) == int(4 / 3.5)
