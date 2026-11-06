#!/usr/bin/env python3
"""Compact golden fixtures: store expected once; pytest rebuilds provider raws."""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Rebuild helper. Outputs under tests/fixtures/fable/. Full traces stay out of git.
REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "tests" / "fixtures" / "fable"
CACHE = REPO / ".fixture-cache"  # local only; not committed
FIX = ROOT
TRACES = REPO / ".fixture-full-traces"  # local only; not committed
FIX.mkdir(parents=True, exist_ok=True)
# Do not create TRACES by default — full traces are intentionally not in the repo.

GOOD = {
    "Write", "Bash", "Read", "Edit", "WebFetch", "WebSearch",
    "TaskCreate", "TaskUpdate", "TaskOutput", "ToolSearch",
    "list_dir", "read_file", "write_file", "bash", "search",
}

def _tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}

TOOL_BANK = {
    "Write": _tool("Write", "Write a file", {"file_path": {"type": "string"}, "content": {"type": "string"}}, ["file_path", "content"]),
    "Bash": _tool("Bash", "Run a shell command", {"command": {"type": "string"}, "description": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]),
    "Read": _tool("Read", "Read a file", {"file_path": {"type": "string"}, "limit": {"type": "number"}, "offset": {"type": "number"}}, ["file_path"]),
    "Edit": _tool("Edit", "Edit a file", {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, ["file_path", "old_string", "new_string"]),
    "WebFetch": _tool("WebFetch", "Fetch a URL", {"url": {"type": "string"}, "prompt": {"type": "string"}}, ["url"]),
    "WebSearch": _tool("WebSearch", "Web search", {"query": {"type": "string"}, "allowed_domains": {"type": "array", "items": {"type": "string"}}}, ["query"]),
    "TaskCreate": _tool("TaskCreate", "Create a task", {"subject": {"type": "string"}, "description": {"type": "string"}, "activeForm": {"type": "string"}}, ["subject"]),
    "TaskUpdate": _tool("TaskUpdate", "Update a task", {"taskId": {"type": "string"}, "status": {"type": "string"}}, ["taskId"]),
    "TaskOutput": _tool("TaskOutput", "Read task output", {"task_id": {"type": "string"}, "block": {"type": "boolean"}, "timeout": {"type": "number"}}, ["task_id"]),
    "ToolSearch": _tool("ToolSearch", "Search tools", {"query": {"type": "string"}, "max_results": {"type": "number"}}, ["query"]),
    "write_file": _tool("write_file", "Write or edit file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    "bash": _tool("bash", "Run a shell command", {"command": {"type": "string"}}, ["command"]),
    "read_file": _tool("read_file", "Read file contents", {"path": {"type": "string"}}, ["path"]),
    "search": _tool("search", "Search", {"query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
    "list_dir": _tool("list_dir", "List a directory", {"path": {"type": "string"}}, ["path"]),
}

THOUGHT_RE = re.compile(r"<thought>.*?</thought>", re.DOTALL | re.IGNORECASE)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"^<[^>]+>$")


def strip_reasoning(text: str | None) -> str:
    if not text:
        return ""
    return THINK_RE.sub("", THOUGHT_RE.sub("", text)).strip()


def parse_args(tc: dict) -> tuple[str, dict, bool]:
    fn = tc.get("function") or {}
    name = fn.get("name") or tc.get("name") or ""
    raw = fn.get("arguments") if "function" in tc else tc.get("arguments")
    if raw is None:
        raw = tc.get("input") or tc.get("args") or {}
    if isinstance(raw, dict):
        return name, raw, True
    if isinstance(raw, str):
        try:
            v = json.loads(raw) if raw.strip() else {}
            return name, v if isinstance(v, dict) else {}, True
        except json.JSONDecodeError:
            return name, {}, False
    return name, {}, False


def truncate_str(s: str, cap: int) -> tuple[str, bool]:
    if len(s) <= cap:
        return s, False
    keep = max(40, (cap - 40) // 2)
    return s[:keep] + f"…<<truncated {len(s) - 2 * keep} chars>>…" + s[-keep:], True


def shrink_args(args: dict, cap: int) -> tuple[dict, bool]:
    out, truncated = {}, False
    for k, v in args.items():
        if isinstance(v, str):
            nv, tr = truncate_str(v, cap)
            out[k] = nv
            truncated |= tr
        elif isinstance(v, list):
            nl = []
            for item in v:
                if isinstance(item, str):
                    nv, tr = truncate_str(item, min(cap, 200))
                    nl.append(nv)
                    truncated |= tr
                else:
                    nl.append(item)
            out[k] = nl
        else:
            out[k] = v
    return out, truncated


def estimate_tokens(obj: Any) -> int:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, int(len(s) / 3.5))


def is_good_turn(m: dict) -> bool:
    if m.get("role") != "assistant" or not m.get("tool_calls"):
        return False
    for tc in m["tool_calls"]:
        name, args, ok = parse_args(tc)
        if not ok or name not in GOOD:
            return False
        if args and all(isinstance(v, str) and PLACEHOLDER_RE.match(v.strip()) for v in args.values() if v is not None):
            return False
    return True


def load_full_rows() -> list[dict]:
    rows = []
    for fname in sorted(CACHE.iterdir()):
        if not fname.name.startswith("full_") or fname.name < "full_05000":
            continue
        data = json.loads(fname.read_text())
        for sr in data.get("slim_rows") or []:
            rows.append({"source_split": "full", "cache_file": fname.name,
                         "row_idx": sr.get("row_idx"), "row": sr["row"]})
    return rows


def tools_for_names(names: set[str]) -> list[dict]:
    out = []
    for n in sorted(names):
        if n in TOOL_BANK:
            out.append(TOOL_BANK[n])
        else:
            out.append(_tool(n, n, {}, []))
    return out


def expected_from_tcs(raw_tcs: list[dict], arg_cap: int) -> tuple[list[dict], bool]:
    expected, truncated = [], False
    for tc in raw_tcs:
        name, args, ok = parse_args(tc)
        assert ok
        args2, tr = shrink_args(args, arg_cap)
        truncated |= tr
        cid = str(tc.get("id") or f"call_{hashlib.md5(name.encode()).hexdigest()[:10]}")
        flat = {"id": cid, "tool": name, "args": dict(args2)}
        for k, v in args2.items():
            if k not in ("id", "tool", "args"):
                flat[k] = v
        expected.append(flat)
    return expected, truncated


def bucket_key(n_calls, names, text, args_list):
    if n_calls == 0:
        return "final"
    size = "1" if n_calls == 1 else ("2-3" if n_calls <= 3 else ("4-8" if n_calls <= 8 else "9+"))
    flags = ["text" if text else "silent"]
    if any(any(isinstance(v, str) and any(ord(c) > 127 for c in v) for v in a.values()) for a in args_list):
        flags.append("u")
    if any(any(isinstance(v, str) and len(v) > 400 for v in a.values()) for a in args_list):
        flags.append("L")
    if any(any(isinstance(v, (dict, list)) for v in a.values()) for a in args_list):
        flags.append("N")
    if len(set(names)) > 1:
        flags.append("M")
    return f"{names[0]}|{size}|{''.join(flags)}"


def arg_bytes(args_list):
    return sum(len(json.dumps(a, ensure_ascii=False)) for a in args_list)


def collect_candidates(full_rows):
    cands = []
    for meta in full_rows:
        msgs = meta["row"]["messages"]
        for mi, m in enumerate(msgs):
            if is_good_turn(m):
                names, args_list = [], []
                for tc in m["tool_calls"]:
                    name, args, _ = parse_args(tc)
                    names.append(name); args_list.append(args)
                text = strip_reasoning(m.get("content"))
                follow = []
                for m2 in msgs[mi+1:]:
                    if m2.get("role") == "tool":
                        follow.append(m2)
                    else:
                        break
                cands.append({
                    "meta": meta, "msg_index": mi, "message": m, "names": names,
                    "args_list": args_list, "text": text, "n_calls": len(names),
                    "bucket": bucket_key(len(names), names, text, args_list),
                    "tool_followups": follow,
                    "abytes": arg_bytes(args_list),
                    "prior_user": next((x.get("content") for x in reversed(msgs[:mi]) if x.get("role")=="user"), ""),
                })
            elif m.get("role")=="assistant" and not m.get("tool_calls"):
                content = strip_reasoning(m.get("content"))
                if len(content) < 40: continue
                cands.append({
                    "meta": meta, "msg_index": mi, "message": m, "names": [],
                    "args_list": [], "text": content, "n_calls": 0, "bucket": "final",
                    "tool_followups": [], "abytes": 0,
                    "prior_user": next((x.get("content") for x in reversed(msgs[:mi]) if x.get("role")=="user"), ""),
                })
    return cands


def sample_turns(cands, target=300, seed=42):
    rng = random.Random(seed)
    by_b = defaultdict(list)
    for c in cands:
        by_b[c["bucket"]].append(c)
    picked = []
    # Prefer smaller-arg turns within each bucket, but keep some long/unicode
    for b, pool in by_b.items():
        pool.sort(key=lambda x: x["abytes"])
        if b == "final":
            take = min(25, len(pool))
            picked.extend(pool[:take])
            continue
        want_small = 2
        want_edge = 1 if (("u" in b) or ("L" in b) or ("N" in b) or ("9+" in b)) else 0
        if "9+" in b:
            want_small, want_edge = 3, 2
        picked.extend(pool[:want_small])
        if want_edge and len(pool) > want_small:
            picked.extend(pool[-want_edge:])
    # fill preferring small abytes and underrepresented tools
    remaining = [c for c in cands if c not in picked]
    remaining.sort(key=lambda c: c["abytes"])
    tool_counts = Counter(n for c in picked for n in (c["names"] or ["_final"]))
    remaining.sort(key=lambda c: (min(tool_counts[n] for n in (c["names"] or ["_final"])), c["abytes"]))
    for c in remaining:
        if len(picked) >= target:
            break
        # skip enormous multi-Write blobs for the compact suite (keep via edge buckets already)
        if c["abytes"] > 50000 and "L" not in c["bucket"]:
            continue
        picked.append(c)
        for n in (c["names"] or ["_final"]):
            tool_counts[n] += 1
    rng.shuffle(picked)
    return picked[:target]


def build_case(c, idx, arg_cap, text_cap, result_cap):
    """Prefer untruncated tool args when the record stays under ~20KB."""
    names = set(c["names"]) if c["n_calls"] else {"Read"}
    tools = tools_for_names(names)
    finish_base = "tool_calls" if c["n_calls"] else "stop"

    def assemble(a_cap, t_cap, r_cap):
        expected, trunc_args = expected_from_tcs(c["message"].get("tool_calls") or [], a_cap)
        text = c["text"]
        trunc_text = False
        if len(text) > t_cap:
            text, trunc_text = truncate_str(text, t_cap)
        prior = strip_reasoning(c["prior_user"]) or "Continue."
        if len(prior) > 240:
            prior, _ = truncate_str(prior, 240)
        messages = [{"role": "user", "content": prior}]
        if expected:
            messages.append({"role": "assistant", "content": text, "tool_calls": [
                {"id": e["id"], "tool": e["tool"], "args": e["args"]} for e in expected
            ]})
        tool_results = []
        trunc_res = False
        for fr, e in zip(c["tool_followups"], expected):
            content = fr.get("content") or ""
            if len(content) > r_cap:
                content, tr = truncate_str(content, r_cap)
                trunc_res |= tr
            tool_results.append({
                "id": fr.get("tool_call_id") or e["id"],
                "tool": fr.get("name") or e["tool"],
                "ok": True,
                "output": content,
            })
        rec = {
            "id": f"fable_{idx:04d}",
            "src": {"split": c["meta"]["source_split"], "cache": c["meta"]["cache_file"],
                    "row": c["meta"]["row_idx"], "msg": c["msg_index"], "bucket": c["bucket"]},
            "tools": tools,
            "truncated": bool(trunc_args),  # only arg truncation (affects parser golden)
            "truncated_text": bool(trunc_text),
            "truncated_results": bool(trunc_res),
            "text": text,
            "tool_calls": expected,
            "finish_reason": "tool_calls" if expected else "stop",
            "messages": messages,
            "tool_results": tool_results,
        }
        return rec, len(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))

    # Try generous caps first for this record
    for caps in [(max(arg_cap, 20000), max(text_cap, 8000), max(result_cap, 1500)),
                 (arg_cap, text_cap, result_cap),
                 (min(arg_cap, 800), min(text_cap, 400), min(result_cap, 120)),
                 (300, 200, 80)]:
        rec, size = assemble(*caps)
        if size <= 20000 or caps[0] <= 300:
            rec["record_bytes"] = size
            return rec
    rec["record_bytes"] = size
    return rec


def materialize_raws_sample(cases: list[dict], n: int = 24) -> list[dict]:
    """Fully materialized multi-protocol raws for a small inspectable sample."""
    out = []
    # pick diverse
    by = defaultdict(list)
    for c in cases:
        by[c["src"]["bucket"].split("|")[0]].append(c)
    picks = []
    for k, pool in by.items():
        picks.extend(pool[:2])
    for c in cases:
        if len(picks) >= n:
            break
        if c not in picks:
            picks.append(c)
    for c in picks[:n]:
        expected = c["tool_calls"]
        text = c["text"]
        openai = {"choices": [{"message": {
            "role": "assistant", "content": text or None,
            "tool_calls": ([{"id": e["id"], "type": "function",
                             "function": {"name": e["tool"], "arguments": json.dumps(e["args"], ensure_ascii=False)}}
                            for e in expected] or None),
        }, "finish_reason": "tool_calls" if expected else "stop"}]}
        if not expected:
            openai["choices"][0]["message"].pop("tool_calls", None)
        anth = {"content": (
            ([{"type": "text", "text": text}] if text else []) +
            [{"type": "tool_use", "id": e["id"], "name": e["tool"], "input": e["args"]} for e in expected]
        ), "stop_reason": "tool_use" if expected else "end_turn"}
        ollama = {"message": {"role": "assistant", "content": text or "",
                              **({"tool_calls": [{"id": e["id"], "function": {"name": e["tool"], "arguments": e["args"]}} for e in expected]} if expected else {})}}
        if expected:
            calls = [{"tool": e["tool"], "id": e["id"], **e["args"]} for e in expected]
            jb = (text.rstrip() + "\n\n" if text else "") + "```looper-tools\n" + json.dumps({"tool_calls": calls}, ensure_ascii=False) + "\n```"
        else:
            jb = text
        out.append({"id": c["id"], "raw": {"openai_chat": openai, "anthropic": anth, "ollama": ollama, "json_block": jb},
                    "expected": {"text": text, "tool_calls": expected, "finish_reason": c["finish_reason"]}})
    return out


def first_user_hash(row):
    for m in row["messages"]:
        if m.get("role") == "user":
            return hashlib.md5((m.get("content") or "")[:2000].encode()).hexdigest()
    return hashlib.md5(b"x").hexdigest()


def tool_sig(row):
    used = Counter()
    for m in row["messages"]:
        if is_good_turn(m):
            for tc in m["tool_calls"]:
                name, _, _ = parse_args(tc)
                used[name] += 1
    return tuple(sorted(used.keys())), used, sum(1 for m in row["messages"] if is_good_turn(m))


def pick_full_traces(full_rows, n=25, seed=7):
    rng = random.Random(seed)
    # unique by first_user_hash; prefer diverse tool signatures and different cache pages
    best_by_hash = {}
    for meta in full_rows:
        row = meta["row"]
        sig, used, good = tool_sig(row)
        if good < 3:
            continue
        h = first_user_hash(row)
        score = (good, len(used), len(row["messages"]))
        prev = best_by_hash.get(h)
        if not prev or score > prev[0]:
            best_by_hash[h] = (score, meta, sig, used)

    items = list(best_by_hash.values())
    rng.shuffle(items)
    # round-robin by primary rare tools
    by_sig = defaultdict(list)
    for score, meta, sig, used in items:
        by_sig[sig].append((score, meta, used))
    for sig in by_sig:
        by_sig[sig].sort(key=lambda x: -x[0][0])

    picked = []
    sigs = list(by_sig.keys())
    # prefer rarer signatures first
    sigs.sort(key=lambda s: (len(by_sig[s]), -len(s)))
    while len(picked) < n and sigs:
        progressed = False
        for sig in list(sigs):
            if by_sig[sig]:
                _score, meta, _used = by_sig[sig].pop(0)
                picked.append(meta)
                progressed = True
                if len(picked) >= n:
                    break
            if not by_sig[sig]:
                sigs.remove(sig)
        if not progressed:
            break
    return picked[:n]


def clean_trace_messages(msgs):
    out = []
    for m in msgs:
        role = m.get("role")
        entry = {"role": role}
        if role == "assistant":
            entry["content"] = strip_reasoning(m.get("content"))
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
        elif role == "tool":
            entry["content"] = m.get("content") or ""
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                entry["name"] = m["name"]
        else:
            entry["content"] = m.get("content") or ""
        out.append(entry)
    return out


def main():
    for folder in (FIX, TRACES):
        if folder.is_dir():
            for p in folder.glob("*"):
                if p.is_file():
                    p.unlink()

    print("Loading…")
    full_rows = load_full_rows()
    cands = collect_candidates(full_rows)
    print(f"rows={len(full_rows)} candidates={len(cands)}")
    turns = sample_turns(cands, target=300)
    print(f"sampled={len(turns)} tools={Counter((t['names'] or ['_f'])[0] for t in turns)}")

    # Adaptive arg caps for <2.8MB
    plans = [(4000, 1500, 400), (1500, 800, 200), (800, 400, 120), (400, 250, 80), (200, 180, 60)]
    cases_path = FIX / "cases.jsonl"
    for arg_cap, text_cap, result_cap in plans:
        cases = [build_case(c, i, arg_cap, text_cap, result_cap) for i, c in enumerate(turns)]
        with cases_path.open("w", encoding="utf-8") as f:
            for rec in cases:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        # sample materialized raws
        samples = materialize_raws_sample(cases, n=24)
        sample_path = FIX / "raw_protocol_samples.jsonl"
        with sample_path.open("w", encoding="utf-8") as f:
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False, separators=(",", ":")) + "\n")
        # shared tool bank once
        bank_path = FIX / "tool_bank.json"
        bank_path.write_text(json.dumps(TOOL_BANK, ensure_ascii=False, separators=(",", ":")))
        total = sum(p.stat().st_size for p in FIX.iterdir() if p.is_file())
        n_trunc = sum(1 for c in cases if c["truncated"])
        print(f"caps={arg_cap}/{text_cap}/{result_cap} bytes={total} trunc={n_trunc}/{len(cases)}")
        if total <= 2_800_000:
            break
    else:
        print("still too big", file=sys.stderr)
        return 2

    # Full traces (written under .fixture-full-traces/; not committed)
    print("traces…")
    TRACES.mkdir(parents=True, exist_ok=True)
    traces = pick_full_traces(full_rows, 25)
    tpath = TRACES / "fable_full_traces.jsonl"
    hashes = []
    with tpath.open("w", encoding="utf-8") as tf:
        for i, meta in enumerate(traces):
            row = meta["row"]
            sig, used, good = tool_sig(row)
            tools = tools_for_names(set(used))
            msgs = clean_trace_messages(row["messages"])
            est = estimate_tokens({"messages": msgs, "tools": tools})
            chars = len(json.dumps({"messages": msgs, "tools": tools}, ensure_ascii=False))
            h = first_user_hash(row)
            hashes.append(h)
            doc = {
                "id": f"trace_{i:02d}",
                "source": {"dataset": "MoreThought/Fable-5.1-Max-Reasoning-Filtered-10000x",
                           "split": meta["source_split"], "cache_file": meta["cache_file"],
                           "row_idx": meta["row_idx"], "license": "Apache-2.0", "attribution": "MoreThought"},
                "tools": tools, "messages": msgs,
                "stats": {"n_messages": len(msgs), "n_assistant_tool_turns": good,
                          "n_final_answers": sum(1 for m in msgs if m.get("role")=="assistant" and not m.get("tool_calls")),
                          "chars": chars, "est_tokens_chars_div_3_5": est,
                          "tools_used": sorted(used), "first_user_md5": h, "tool_sig": list(sig)},
                "note": "Complete multi-turn trace for replay/eval; not part of the compact parser suite. Thought tags stripped; tool_calls/results untruncated.",
            }
            tf.write(json.dumps(doc, ensure_ascii=False) + "\n")
            print(f"  {doc['id']}: msgs={len(msgs)} tok~{est} sig={list(sig)} hash={h[:8]}")
    print(f"unique hashes={len(set(hashes))}/{len(hashes)} traces_bytes={tpath.stat().st_size}")

    (FIX / "manifest.json").write_text(json.dumps({
        "n_cases": len(cases),
        "n_raw_samples": len(samples),
        "protocols_covered": ["openai_chat", "anthropic", "ollama", "json_block"],
        "fixtures_bytes": total,
        "dataset_row_counts": {
            "train_split": 5000,
            "full_split": 10000,
            "sum_across_splits_viewer": 15000,
            "card_claim": 10000,
            "note": "HF size API reports 15000 = train(5k)+full(10k); card describes the full 10k split.",
        },
        "arg_caps": {"arg": arg_cap, "text": text_cap, "result": result_cap},
        "raw_materialization": "Provider-shaped raws are rebuilt in the pytest from expected tool_calls (deterministic). raw_protocol_samples.jsonl holds 24 fully-materialized multi-protocol examples for inspection.",
    }, indent=2))
    print("OK fixtures_bytes", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
