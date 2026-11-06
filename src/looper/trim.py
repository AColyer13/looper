"""Budget-triggered conversation history trimming for ``looper ask``.

Opt-in (``history_trim.enabled``, default false). No new dependencies.
Token estimate = total chars / ``chars_per_token`` (default 3.5).

Pipeline when over budget (1→2→3, stop early when under):
  1. Stub Write/write_file (and trivial bash-heredoc writes) older than
     ``write_recent_turns``.
  2. Head+tail old tool results (keep error-matching lines).
  3. Deterministic ledger collapse of the middle turns.

Caller-owned history is never mutated for the response ``messages`` field;
ask() returns a ``trim`` report describing what was sent to the provider.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

ERR_LINE_RE = re.compile(
    r"(error|fail(?:ed|ure)?|exception|traceback|assert|panic|FAILED|Error:|ERROR)",
    re.IGNORECASE,
)

# Trivial bash heredoc write: `cat > path <<EOF` / `tee > path <<'EOF'`
_BASH_HEREDOC_WRITE = re.compile(
    r"""(?mx)
    ^\s*(?:sudo\s+)?(?:cat|tee)\s+
    (?:(?P<redir1>>{1,2})\s*(?P<path1>\S+)\s+)?
    <<\s*['"]?(?P<delim>\w+)['"]?
    (?:\s+(?P<redir2>>{1,2})\s*(?P<path2>\S+))?
    """,
)

WRITE_TOOLS = frozenset({"Write", "write_file"})
EDIT_TOOLS = frozenset({"Edit", "edit_file"})
READ_TOOLS = frozenset({"Read", "read_file"})
BASH_TOOLS = frozenset({"Bash", "bash", "run_command", "shell"})


@dataclass
class HistoryTrimSpec:
    enabled: bool = False
    budget_fill: float = 0.5
    write_recent_turns: int = 20
    result_keep_recent_turns: int = 8
    head_lines: int = 40
    tail_lines: int = 40
    max_tool_result_lines: int = 100
    keep_error_lines: bool = True
    keep_recent_turns: int = 20

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_history_trim(raw: Any, *, base: HistoryTrimSpec | None = None) -> HistoryTrimSpec:
    """Build a HistoryTrimSpec from config / request JSON. ``False`` disables."""
    b = base or HistoryTrimSpec()
    if raw is False or raw is None:
        return HistoryTrimSpec(**{**asdict(b), "enabled": False}) if raw is False else HistoryTrimSpec(**asdict(b))
    if raw is True:
        return HistoryTrimSpec(**{**asdict(b), "enabled": True})
    if not isinstance(raw, dict):
        raise ValueError("history_trim must be an object, true, false, or omitted")
    d = asdict(b)
    for k in d:
        if k in raw:
            d[k] = raw[k]
    # coerce types
    d["enabled"] = bool(d["enabled"])
    d["budget_fill"] = float(d["budget_fill"])
    d["write_recent_turns"] = int(d["write_recent_turns"])
    d["result_keep_recent_turns"] = int(d["result_keep_recent_turns"])
    d["head_lines"] = int(d["head_lines"])
    d["tail_lines"] = int(d["tail_lines"])
    d["max_tool_result_lines"] = int(d["max_tool_result_lines"])
    d["keep_error_lines"] = bool(d["keep_error_lines"])
    d["keep_recent_turns"] = int(d["keep_recent_turns"])
    if not 0 < d["budget_fill"] <= 1:
        raise ValueError("history_trim.budget_fill must be in (0, 1]")
    for key in ("write_recent_turns", "result_keep_recent_turns", "keep_recent_turns",
                "head_lines", "tail_lines", "max_tool_result_lines"):
        if d[key] < 0:
            raise ValueError(f"history_trim.{key} must be >= 0")
    return HistoryTrimSpec(**d)


def merge_history_trim(base: HistoryTrimSpec, override: Any) -> HistoryTrimSpec:
    """Apply a per-request override on top of config."""
    if override is None:
        return HistoryTrimSpec(**asdict(base))
    if override is False:
        return HistoryTrimSpec(**{**asdict(base), "enabled": False})
    if override is True:
        return HistoryTrimSpec(**{**asdict(base), "enabled": True})
    return parse_history_trim(override, base=base)


# --------------------------------------------------------------------------- helpers


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def _n_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _msg_chars(msg: dict[str, Any]) -> int:
    n = 0
    c = msg.get("content")
    if isinstance(c, str):
        n += len(c)
    elif isinstance(c, list):
        # Anthropic-style content blocks on provider-neutral messages (rare pre-convert)
        n += len(json.dumps(c, ensure_ascii=False))
    for tc in msg.get("tool_calls") or []:
        n += len(json.dumps(tc, ensure_ascii=False))
    return n


def estimate_tokens(messages: list[dict[str, Any]], chars_per_token: float = 3.5) -> int:
    return max(0, int(sum(_msg_chars(m) for m in messages) / chars_per_token))


def parse_tc(tc: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """(name, args, call_id) from looper-neutral or OpenAI-shaped tool_calls."""
    cid = str(tc.get("id") or tc.get("call_id") or "")
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
    if fn:
        name = fn.get("name") or ""
        raw = fn.get("arguments")
    else:
        name = tc.get("tool") or tc.get("name") or ""
        raw = tc.get("args") if "args" in tc else tc.get("arguments")
        if raw is None:
            raw = tc.get("input")
        if raw is None:
            raw = {
                k: v for k, v in tc.items()
                if k not in ("id", "call_id", "tool", "name", "args", "arguments", "input", "type", "function")
            }
    if isinstance(raw, str):
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            args = {"_raw": raw}
    elif isinstance(raw, dict):
        args = dict(raw)
    else:
        args = {}
    return str(name), args, cid


def set_tc_args(tc: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    out = dict(tc)
    if isinstance(out.get("function"), dict):
        fn = dict(out["function"])
        fn["arguments"] = json.dumps(args, ensure_ascii=False)
        out["function"] = fn
    else:
        out["args"] = args
        # flattened convenience fields (looper format_tool_call)
        for k in list(out):
            if k not in ("id", "tool", "name", "args", "arguments", "type", "function", "call_id"):
                if k not in args:
                    out.pop(k, None)
        for k, v in args.items():
            if k not in ("id", "tool", "args"):
                out[k] = v
        if "arguments" in out:
            out["arguments"] = args
    return out


def _path_from_write_args(args: dict[str, Any]) -> str | None:
    p = args.get("file_path") or args.get("path")
    return str(p) if p else None


def _content_from_write_args(args: dict[str, Any]) -> str:
    c = args.get("content")
    return c if isinstance(c, str) else ""


def _align_cut(messages: list[dict[str, Any]], cut: int) -> int:
    """Move cut earlier so the suffix never starts on an orphan tool result."""
    if cut <= 0 or cut >= len(messages):
        return cut
    while cut > 0 and messages[cut].get("role") == "tool":
        cut -= 1
    return cut


def pairing_violations(messages: list[dict[str, Any]]) -> list[str]:
    """Return human-readable pairing problems (empty = ok).

    Understands looper-neutral ``tool_calls`` / ``role=tool`` and Anthropic
    content-block ``tool_use`` / ``tool_result`` shapes.
    """
    issues: list[str] = []
    pending: dict[str, int] = {}
    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                _, _, cid = parse_tc(tc)
                if cid:
                    pending[cid] = i
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        cid = str(block.get("id") or "")
                        if cid:
                            pending[cid] = i
        elif role == "tool":
            cid = str(msg.get("tool_call_id") or msg.get("id") or "")
            if not cid:
                issues.append(f"messages[{i}]: tool result missing tool_call_id")
                continue
            if cid not in pending:
                issues.append(f"messages[{i}]: orphan tool result id={cid}")
            else:
                del pending[cid]
        elif role == "user" and isinstance(content, list):
            saw_tool_result = False
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    saw_tool_result = True
                    cid = str(block.get("tool_use_id") or block.get("tool_call_id") or "")
                    if not cid:
                        issues.append(f"messages[{i}]: tool_result missing tool_use_id")
                        continue
                    if cid not in pending:
                        issues.append(f"messages[{i}]: orphan tool_result id={cid}")
                    else:
                        del pending[cid]
            if not saw_tool_result and pending:
                issues.append(
                    f"messages[{i}]: {len(pending)} tool_call(s) without results before user"
                )
                pending.clear()
        elif role in ("user", "system") and pending:
            issues.append(
                f"messages[{i}]: {len(pending)} tool_call(s) without results before {role}"
            )
            pending.clear()
    return issues


# --------------------------------------------------------------------------- 1) stub old writes


def stub_writes_older_than(
    messages: list[dict[str, Any]], recent_turns: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out = deepcopy(messages)
    cutoff = max(0, len(out) - recent_turns)
    stubbed = 0
    for mi, msg in enumerate(out):
        if mi >= cutoff or msg.get("role") != "assistant":
            continue
        tcs = msg.get("tool_calls") or []
        if not tcs:
            continue
        new_tcs = []
        changed = False
        for tc in tcs:
            name, args, _cid = parse_tc(tc)
            if name in WRITE_TOOLS:
                body = _content_from_write_args(args)
                path = _path_from_write_args(args) or "?"
                if body and not body.startswith("[wrote "):
                    stub = (
                        f"[wrote {path}, {_n_lines(body)} lines, sha8={_sha8(body)} "
                        f"— content omitted]"
                    )
                    new_args = dict(args)
                    new_args["content"] = stub
                    new_tcs.append(set_tc_args(tc, new_args))
                    stubbed += 1
                    changed = True
                    continue
            elif name in BASH_TOOLS:
                cmd = args.get("command") if isinstance(args.get("command"), str) else ""
                new_cmd = _stub_bash_heredoc_write(cmd)
                if new_cmd is not None and new_cmd != cmd:
                    new_args = dict(args)
                    new_args["command"] = new_cmd
                    new_tcs.append(set_tc_args(tc, new_args))
                    stubbed += 1
                    changed = True
                    continue
            new_tcs.append(tc)
        if changed:
            out[mi] = dict(msg)
            out[mi]["tool_calls"] = new_tcs
        # Anthropic-style tool_use Write blocks in content
        content = out[mi].get("content") if changed else msg.get("content")
        if isinstance(content, list):
            new_blocks = []
            block_changed = False
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") in WRITE_TOOLS
                ):
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    body = _content_from_write_args(inp)
                    path = _path_from_write_args(inp) or "?"
                    if body and not body.startswith("[wrote "):
                        stub = (
                            f"[wrote {path}, {_n_lines(body)} lines, sha8={_sha8(body)} "
                            f"— content omitted]"
                        )
                        nb = dict(block)
                        ni = dict(inp)
                        ni["content"] = stub
                        nb["input"] = ni
                        new_blocks.append(nb)
                        stubbed += 1
                        block_changed = True
                        continue
                new_blocks.append(block)
            if block_changed:
                out[mi] = dict(out[mi]) if changed else dict(msg)
                out[mi]["content"] = new_blocks
    return out, {"stubbed": stubbed}


def _stub_bash_heredoc_write(cmd: str) -> str | None:
    """If cmd is a trivial cat/tee heredoc write, replace the body with a stub.

    Returns None when not trivially detectable (caller keeps original).
    """
    if not cmd or "<<" not in cmd:
        return None
    m = _BASH_HEREDOC_WRITE.search(cmd)
    if not m:
        return None
    path = m.group("path1") or m.group("path2")
    delim = m.group("delim")
    if not path or not delim:
        return None
    # Find heredoc body after the opening line
    open_end = m.end()
    # Body starts at next newline after match
    nl = cmd.find("\n", open_end - 1)
    if nl < 0:
        return None
    body_start = nl + 1
    closer = re.search(rf"(?m)^{re.escape(delim)}\s*$", cmd[body_start:])
    if not closer:
        return None
    body = cmd[body_start : body_start + closer.start()]
    if _n_lines(body) < 5 and len(body) < 200:
        return None  # tiny — not worth stubbing
    stub_body = (
        f"[wrote {path}, {_n_lines(body)} lines, sha8={_sha8(body)} — content omitted]\n"
    )
    new_cmd = cmd[:body_start] + stub_body + cmd[body_start + closer.start() :]
    return new_cmd


# --------------------------------------------------------------------------- 2) truncate old tool results


def _head_tail_plus_errors(text: str, head: int, tail: int, *, keep_errors: bool) -> str:
    lines = text.splitlines(keepends=True)
    if len(lines) <= head + tail:
        return text
    keep: set[int] = set(range(head)) | set(range(len(lines) - tail, len(lines)))
    if keep_errors:
        for i, line in enumerate(lines):
            if ERR_LINE_RE.search(line):
                for j in range(max(0, i - 1), min(len(lines), i + 2)):
                    keep.add(j)
    out: list[str] = []
    prev = -1
    for i in sorted(keep):
        if prev >= 0 and i > prev + 1:
            out.append(f"…[{i - prev - 1} lines elided]…\n")
        out.append(lines[i])
        prev = i
    return "".join(out)


def truncate_old_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int,
    head: int,
    tail: int,
    min_lines: int,
    keep_errors: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out = deepcopy(messages)
    cutoff = max(0, len(out) - keep_recent)
    trimmed = 0
    for mi, msg in enumerate(out):
        if mi >= cutoff:
            continue
        role = msg.get("role")
        if role == "tool":
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            lines = content.splitlines(keepends=True)
            if len(lines) <= min_lines:
                continue
            new_c = _head_tail_plus_errors(content, head, tail, keep_errors=keep_errors)
            if len(new_c) >= len(content):
                continue
            out[mi] = dict(msg)
            out[mi]["content"] = new_c
            trimmed += 1
        elif role == "user" and isinstance(msg.get("content"), list):
            new_blocks = []
            block_changed = False
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    c = block.get("content")
                    text = c if isinstance(c, str) else (
                        "".join(
                            (p.get("text") or "") for p in c
                            if isinstance(p, dict)
                        ) if isinstance(c, list) else ""
                    )
                    if isinstance(text, str) and len(text.splitlines()) > min_lines:
                        new_c = _head_tail_plus_errors(text, head, tail, keep_errors=keep_errors)
                        if len(new_c) < len(text):
                            nb = dict(block)
                            nb["content"] = new_c
                            new_blocks.append(nb)
                            trimmed += 1
                            block_changed = True
                            continue
                new_blocks.append(block)
            if block_changed:
                out[mi] = dict(msg)
                out[mi]["content"] = new_blocks
    return out, {"trimmed": trimmed}


# --------------------------------------------------------------------------- 3) collapse middle


def _build_ledger(middle: list[dict[str, Any]]) -> str:
    files: dict[str, str] = {}
    commands: list[str] = []
    errors: list[str] = []
    id2name: dict[str, str] = {}

    for msg in middle:
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                name, args, cid = parse_tc(tc)
                if cid:
                    id2name[cid] = name
                if name in WRITE_TOOLS | EDIT_TOOLS:
                    path = _path_from_write_args(args) or "?"
                    if name in EDIT_TOOLS:
                        files[str(path)] = "edited"
                    else:
                        body = _content_from_write_args(args)
                        files[str(path)] = f"wrote sha8={_sha8(body)} lines={_n_lines(body)}"
                elif name in READ_TOOLS:
                    path = _path_from_write_args(args) or "?"
                    files.setdefault(str(path), "read")
                elif name in BASH_TOOLS:
                    cmd = args.get("command") if isinstance(args.get("command"), str) else ""
                    one = cmd.strip().splitlines()[0][:120] if cmd.strip() else "(empty)"
                    commands.append(one)
        elif role == "tool":
            content = msg.get("content") if isinstance(msg.get("content"), str) else ""
            cid = str(msg.get("tool_call_id") or "")
            name = id2name.get(cid, str(msg.get("tool") or msg.get("name") or ""))
            if name in BASH_TOOLS and commands:
                status = "error?" if content and ERR_LINE_RE.search(content[:2000]) else "ok"
                commands[-1] = f"{commands[-1]}  [{status}]"
            if content and ERR_LINE_RE.search(content[:2000] or ""):
                for ln in content.splitlines():
                    if ERR_LINE_RE.search(ln):
                        errors.append(ln[:160])
                        break

    parts = ["[trimmed ledger — older turns compacted]"]
    if files:
        parts.append("files:")
        for p, note in list(files.items())[:80]:
            parts.append(f"  - {p}: {note}")
        if len(files) > 80:
            parts.append(f"  …[{len(files) - 80} more files]")
    if commands:
        parts.append("commands:")
        for c in commands[:40]:
            parts.append(f"  $ {c}")
        if len(commands) > 40:
            parts.append(f"  …[{len(commands) - 40} more]")
    if errors:
        parts.append("errors_seen:")
        for e in errors[:20]:
            parts.append(f"  ! {e}")
    return "\n".join(parts)


def collapse_middle(
    messages: list[dict[str, Any]], *, keep_recent: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(messages) <= keep_recent + 3:
        return deepcopy(messages), {"collapsed": False, "dropped": 0}

    prefix: list[dict[str, Any]] = []
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        prefix.append(deepcopy(messages[i]))
        i += 1
    while i < len(messages) and messages[i].get("role") != "user":
        if messages[i].get("role") == "assistant":
            break
        i += 1
    if i < len(messages) and messages[i].get("role") == "user":
        prefix.append(deepcopy(messages[i]))
        i += 1

    cut = _align_cut(messages, len(messages) - keep_recent)
    if cut <= i:
        return deepcopy(messages), {"collapsed": False, "dropped": 0}

    middle = messages[i:cut]
    suffix = [deepcopy(m) for m in messages[cut:]]
    ledger = {"role": "user", "content": _build_ledger(middle)}
    out = prefix + [ledger] + suffix
    return out, {"collapsed": True, "dropped": len(middle)}


# --------------------------------------------------------------------------- pipeline


@dataclass
class TrimReport:
    applied: bool = False
    before_tokens: int = 0
    after_tokens: int = 0
    budget: int | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "budget": self.budget,
            "steps": list(self.steps),
        }


def trim_history(
    messages: list[dict[str, Any]],
    *,
    context_window: int | None,
    cfg: HistoryTrimSpec,
    chars_per_token: float = 3.5,
    token_fn: Callable[[list[dict[str, Any]]], int] | None = None,
) -> tuple[list[dict[str, Any]], TrimReport]:
    """Return (messages_for_provider, report). Input list is never mutated."""
    count = token_fn or (lambda ms: estimate_tokens(ms, chars_per_token))
    before = count(messages)
    report = TrimReport(applied=False, before_tokens=before, after_tokens=before, budget=None)

    if not cfg.enabled:
        return messages, report
    if not context_window or context_window <= 0:
        return messages, report

    budget = int(context_window * cfg.budget_fill)
    report.budget = budget
    if before <= budget:
        return messages, report

    out = messages
    report.applied = True

    def over(ms: list[dict[str, Any]]) -> bool:
        return count(ms) > budget

    out, st = stub_writes_older_than(out, cfg.write_recent_turns)
    tok = count(out)
    report.steps.append({"lever": "writes", "tokens": tok, **st})
    if not over(out):
        report.after_tokens = tok
        return out, report

    out, st = truncate_old_tool_results(
        out,
        keep_recent=cfg.result_keep_recent_turns,
        head=cfg.head_lines,
        tail=cfg.tail_lines,
        min_lines=cfg.max_tool_result_lines,
        keep_errors=cfg.keep_error_lines,
    )
    tok = count(out)
    report.steps.append({"lever": "results", "tokens": tok, **st})
    if not over(out):
        report.after_tokens = tok
        return out, report

    out, st = collapse_middle(out, keep_recent=cfg.keep_recent_turns)
    tok = count(out)
    report.steps.append({"lever": "collapse", "tokens": tok, **st})
    report.after_tokens = tok
    return out, report
