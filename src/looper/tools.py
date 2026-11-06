"""Caller-owned tool calling for ``looper ask``.

looper never executes tools. It only:
* offers tool schemas to a model (native function calling or a fenced JSON block)
* returns structured ``tool_calls`` for the caller to run
* accepts ``tool_results`` on a follow-up turn so the model can continue

See ``docs/FRONTEND_API.md`` and ``docs/tool_call.schema.json``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

# Styles that support native function / tool calling in http_runner.
NATIVE_STYLES = frozenset({"anthropic", "openai", "openai_responses", "ollama"})

# Default max tool calls when the tier is "small" (json_block).
SMALL_TIER_MAX_TOOL_CALLS = 2

JSON_BLOCK_FENCE = "looper-tools"
_FENCE_RE = re.compile(
    rf"```(?:{JSON_BLOCK_FENCE}|json)\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_BARE_OBJ_RE = re.compile(
    r"\{\s*\"tool_calls\"\s*:\s*\[.*?\]\s*\}",
    re.DOTALL,
)


@dataclass
class ToolSpec:
    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }


def parse_tools(raw: Any) -> list[ToolSpec]:
    """Normalize a tools list from a request body / CLI JSON."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tools must be a JSON array")
    out: list[ToolSpec] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"tools[{i}] must be an object")
        name = item.get("name") or (item.get("function") or {}).get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"tools[{i}].name is required")
        if name in seen:
            raise ValueError(f"duplicate tool name: {name}")
        seen.add(name)
        desc = item.get("description") or (item.get("function") or {}).get("description") or ""
        params = (
            item.get("parameters")
            or item.get("input_schema")
            or (item.get("function") or {}).get("parameters")
            or {"type": "object", "properties": {}}
        )
        if not isinstance(params, dict):
            raise ValueError(f"tools[{i}].parameters must be a JSON Schema object")
        out.append(ToolSpec(name=name, description=str(desc), parameters=params))
    return out


def choose_protocol(
    *,
    tools: list[ToolSpec],
    agent_kind: str,
    api_style: str | None,
    tier_name: str | None,
    structured_output: str | None = None,
    force: str | None = None,
) -> tuple[str, list[str]]:
    """Return ``(protocol, warnings)`` where protocol is native|json_block|none.

    Tier policy:
    * tiny → none (tools refused)
    * small → json_block
    * medium+ with native-capable HTTP style → native
    * CLI agents → always json_block when tools are offered (no native API)
    """
    warnings: list[str] = []
    if not tools:
        return "none", warnings
    if force in ("native", "json_block", "none"):
        if force == "native" and (agent_kind == "cli" or (api_style or "") not in NATIVE_STYLES):
            warnings.append(
                f"forced protocol 'native' not available for kind={agent_kind} style={api_style}; using json_block"
            )
            return "json_block", warnings
        return force, warnings

    tier = (tier_name or "").lower()
    if tier == "tiny":
        warnings.append(
            "tier 'tiny' cannot use tools (structured output unreliable); returning text only"
        )
        return "none", warnings

    if agent_kind == "cli":
        return "json_block", warnings

    style = api_style or "openai"
    so = (structured_output or "good").lower()
    if tier == "small" or so in ("unreliable", "fair"):
        return "json_block", warnings
    if style in NATIVE_STYLES:
        return "native", warnings
    warnings.append(f"api_style '{style}' has no native tools mapping; using json_block")
    return "json_block", warnings


def max_tool_calls_for(
    protocol: str,
    tier_name: str | None,
    requested: int | None,
) -> int | None:
    if protocol == "none":
        return 0
    if requested is not None:
        return max(0, int(requested))
    if (tier_name or "").lower() == "small":
        return SMALL_TIER_MAX_TOOL_CALLS
    return None  # unlimited (provider / model decides)


def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def format_tool_call(tool: str, args: dict[str, Any], *, call_id: str | None = None) -> dict[str, Any]:
    """Flattened fields + ``args`` object + ``id`` (owner example shape + args)."""
    cid = call_id or new_call_id()
    flat = {"id": cid, "tool": tool, "args": dict(args or {})}
    for k, v in (args or {}).items():
        if k in ("id", "tool", "args"):
            continue
        flat[k] = v
    return flat


def validate_tool_calls(
    raw_calls: list[Any],
    tools: list[ToolSpec],
    *,
    max_calls: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate and normalize tool calls. Invalid ones become warnings (not returned)."""
    by_name = {t.name: t for t in tools}
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    if not isinstance(raw_calls, list):
        return [], ["tool_calls must be an array"]
    for i, item in enumerate(raw_calls):
        if max_calls is not None and len(out) >= max_calls:
            warnings.append(f"truncated tool_calls after {max_calls} (tier/request limit)")
            break
        if not isinstance(item, dict):
            warnings.append(f"tool_calls[{i}] is not an object; skipped")
            continue
        name = item.get("tool") or item.get("name") or item.get("function")
        if isinstance(name, dict):
            name = name.get("name")
        if not name or not isinstance(name, str):
            warnings.append(f"tool_calls[{i}] missing tool name; skipped")
            continue
        if name not in by_name:
            warnings.append(f"unknown tool '{name}' in tool_calls[{i}]; skipped")
            continue
        args = item.get("args")
        if args is None:
            args = item.get("arguments") or item.get("input") or item.get("parameters")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                warnings.append(f"tool_calls[{i}] ({name}): arguments are not valid JSON; skipped")
                continue
        if args is None:
            # flattened style: everything except id/tool/name/args/arguments
            args = {
                k: v
                for k, v in item.items()
                if k
                not in (
                    "id",
                    "tool",
                    "name",
                    "args",
                    "arguments",
                    "input",
                    "parameters",
                    "type",
                    "function",
                    "call_id",
                )
            }
        if not isinstance(args, dict):
            warnings.append(f"tool_calls[{i}] ({name}): args must be an object; skipped")
            continue
        # Basic schema checks: required keys if present
        schema = by_name[name].parameters or {}
        required = schema.get("required") or []
        if isinstance(required, list):
            missing = [r for r in required if r not in args]
            if missing:
                warnings.append(
                    f"tool_calls[{i}] ({name}): missing required args {missing}; skipped"
                )
                continue
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            unknown = [k for k in args if k not in props]
            if unknown:
                warnings.append(
                    f"tool_calls[{i}] ({name}): unknown args {unknown} (kept; schema is advisory)"
                )
        cid = item.get("id") or item.get("call_id") or new_call_id()
        out.append(format_tool_call(name, args, call_id=str(cid)))
    return out, warnings


def _loads_lenient(text: str) -> Any:
    """json.loads with a couple of common model mistakes (trailing commas)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(cleaned)


def parse_json_block(text: str, tools: list[ToolSpec], *, max_calls: int | None = None) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Strip a `` ```looper-tools `` block (or bare {tool_calls:...}) from *text*.

    Returns ``(clean_text, tool_calls, warnings)``.
    """
    warnings: list[str] = []
    if not text:
        return "", [], warnings
    body = None
    cleaned = text
    m = _FENCE_RE.search(text)
    if m:
        body = m.group("body").strip()
        cleaned = (text[: m.start()] + text[m.end() :]).strip()
    else:
        m2 = _BARE_OBJ_RE.search(text)
        if m2:
            body = m2.group(0)
            cleaned = (text[: m2.start()] + text[m2.end() :]).strip()
    if body is None:
        return text.strip(), [], warnings
    try:
        data = _loads_lenient(body)
    except json.JSONDecodeError as exc:
        warnings.append(f"malformed looper-tools JSON block: {exc}")
        return text.strip(), [], warnings
    if isinstance(data, dict) and "tool_calls" in data:
        raw = data.get("tool_calls") or []
    elif isinstance(data, list):
        raw = data
    else:
        warnings.append("looper-tools block must be {\"tool_calls\": [...]} or an array")
        return cleaned, [], warnings
    calls, w = validate_tool_calls(raw, tools, max_calls=max_calls)
    warnings.extend(w)
    return cleaned, calls, warnings


def json_block_instructions(tools: list[ToolSpec], *, max_calls: int | None = None) -> str:
    """System / prompt appendix for the text protocol."""
    catalog = [
        {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in tools
    ]
    limit = (
        f"Emit at most {max_calls} tool call(s)."
        if max_calls is not None
        else "Emit as many tool calls as you need (prefer few)."
    )
    return (
        "You may request actions by emitting a single fenced JSON block. "
        "The host application owns the tools and will execute them; you must NOT "
        "edit files or run shell commands yourself.\n\n"
        f"Available tools (JSON Schema):\n{json.dumps(catalog, indent=2)}\n\n"
        f"{limit}\n"
        "When you need a tool, end your reply with:\n\n"
        f"```{JSON_BLOCK_FENCE}\n"
        '{"tool_calls":[{"tool":"TOOL_NAME","arg1":"...","arg2":"..."}]}\n'
        "```\n\n"
        "Rules:\n"
        "* Put any natural-language reply BEFORE the fence.\n"
        "* Use only tool names from the list above.\n"
        "* Arguments must match each tool's parameters schema.\n"
        "* If no tool is needed, reply with plain text and omit the fence.\n"
        "* Do not invent tools. Do not wrap the fence in another code block.\n"
    )


def normalize_messages(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("messages must be a JSON array")
    out: list[dict[str, Any]] = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ValueError(f"messages[{i}] must be an object")
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"messages[{i}].role must be system|user|assistant|tool")
        entry: dict[str, Any] = {"role": role}
        if "content" in m and m["content"] is not None:
            entry["content"] = m["content"] if isinstance(m["content"], str) else str(m["content"])
        else:
            entry["content"] = ""
        if role == "assistant" and m.get("tool_calls"):
            # keep already-normalized or re-normalize lightly
            tcs = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("tool") or tc.get("name")
                args = tc.get("args") or tc.get("arguments") or {
                    k: v for k, v in tc.items() if k not in ("id", "tool", "name", "args", "arguments")
                }
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                tcs.append(format_tool_call(str(name), dict(args or {}), call_id=tc.get("id") or tc.get("call_id")))
            entry["tool_calls"] = tcs
        if role == "tool":
            entry["tool_call_id"] = m.get("tool_call_id") or m.get("id") or ""
            if m.get("tool"):
                entry["tool"] = m["tool"]
            if "ok" in m:
                entry["ok"] = bool(m["ok"])
        out.append(entry)
    return out


def apply_tool_results(
    messages: list[dict[str, Any]],
    tool_results: list[Any] | None,
) -> list[dict[str, Any]]:
    """Append provider-neutral tool result messages."""
    if not tool_results:
        return list(messages)
    out = list(messages)
    for i, tr in enumerate(tool_results):
        if not isinstance(tr, dict):
            raise ValueError(f"tool_results[{i}] must be an object")
        cid = tr.get("id") or tr.get("tool_call_id")
        if not cid:
            raise ValueError(f"tool_results[{i}].id is required")
        ok = tr.get("ok", "error" not in tr)
        content = tr.get("output")
        if content is None:
            content = tr.get("content")
        if content is None and tr.get("error"):
            content = str(tr["error"])
            ok = False
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = json.dumps(content, default=str)
        entry: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": str(cid),
            "content": content,
            "ok": bool(ok),
        }
        if tr.get("tool"):
            entry["tool"] = tr["tool"]
        if not ok and tr.get("error"):
            entry["error"] = str(tr["error"])
        out.append(entry)
    return out


def seed_messages(
    *,
    prompt: str | None,
    system: str | None,
    messages: list[dict[str, Any]] | None,
    tool_results: list[Any] | None,
) -> list[dict[str, Any]]:
    """Build the conversation for this ask turn."""
    msgs = normalize_messages(messages) if messages else []
    if system:
        # ensure a system message at the front (replace existing leading system)
        if msgs and msgs[0]["role"] == "system":
            msgs[0] = {"role": "system", "content": system}
        else:
            msgs = [{"role": "system", "content": system}, *msgs]
    if prompt:
        # Avoid duplicating if the last user message is identical
        if not (msgs and msgs[-1].get("role") == "user" and msgs[-1].get("content") == prompt):
            msgs.append({"role": "user", "content": prompt})
    msgs = apply_tool_results(msgs, tool_results)
    if not any(m["role"] in ("user", "tool", "assistant") for m in msgs):
        raise ValueError("ask requires a prompt, messages, or tool_results")
    return msgs


def append_assistant_message(
    messages: list[dict[str, Any]],
    text: str,
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entry: dict[str, Any] = {"role": "assistant", "content": text or ""}
    if tool_calls:
        entry["tool_calls"] = tool_calls
    return [*messages, entry]


# --------------------------------------------------------------------------- provider adapters (request side)


def tools_for_anthropic(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description or t.name, "input_schema": t.parameters}
        for t in tools
    ]


def tools_for_openai_chat(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or t.name,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def tools_for_openai_responses(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description or t.name,
            "parameters": t.parameters,
        }
        for t in tools
    ]


def tools_for_ollama(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    # Ollama accepts OpenAI-style tool definitions
    return tools_for_openai_chat(tools)


def _content_str(m: dict[str, Any]) -> str:
    c = m.get("content")
    return "" if c is None else str(c)


def messages_to_anthropic(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system_parts.append(_content_str(m))
            continue
        if role == "user":
            out.append({"role": "user", "content": _content_str(m)})
            continue
        if role == "assistant":
            content: list[dict[str, Any]] = []
            text = _content_str(m)
            if text:
                content.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["tool"],
                        "input": tc.get("args") or {},
                    }
                )
            if not content:
                content.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": content})
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": _content_str(m),
            }
            if m.get("ok") is False:
                block["is_error"] = True
            # Anthropic wants tool_result blocks in a user message; merge consecutive tools
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return ("\n\n".join(system_parts) if system_parts else None), out


def messages_to_openai_chat(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            out.append({"role": "system", "content": _content_str(m)})
        elif role == "user":
            out.append({"role": "user", "content": _content_str(m)})
        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": _content_str(m) or None}
            tcs = m.get("tool_calls") or []
            if tcs:
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["tool"],
                            "arguments": json.dumps(tc.get("args") or {}),
                        },
                    }
                    for tc in tcs
                ]
            out.append(entry)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or "",
                    "content": _content_str(m),
                }
            )
    return out


def messages_to_openai_responses(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten provider-neutral messages into Responses API ``input`` items."""
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            items.append({"role": "system", "content": _content_str(m)})
        elif role == "user":
            items.append({"role": "user", "content": _content_str(m)})
        elif role == "assistant":
            text = _content_str(m)
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in m.get("tool_calls") or []:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc["id"],
                        "name": tc["tool"],
                        "arguments": json.dumps(tc.get("args") or {}),
                    }
                )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id") or "",
                    "output": _content_str(m),
                }
            )
    return items


def messages_to_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Ollama chat API is OpenAI-like; tool results use role=tool
    return messages_to_openai_chat(messages)


# --------------------------------------------------------------------------- provider adapters (response side)


def extract_native_anthropic(data: dict[str, Any], tools: list[ToolSpec], *, max_calls: int | None) -> tuple[str, list[dict[str, Any]], str, list[str]]:
    text_parts: list[str] = []
    raw_calls: list[dict[str, Any]] = []
    for b in data.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text_parts.append(b.get("text") or "")
        elif b.get("type") == "tool_use":
            raw_calls.append(
                {
                    "id": b.get("id"),
                    "tool": b.get("name"),
                    "args": b.get("input") or {},
                }
            )
    calls, warnings = validate_tool_calls(raw_calls, tools, max_calls=max_calls)
    stop = data.get("stop_reason") or "end_turn"
    if calls and stop in ("tool_use", "end_turn", None):
        finish = "tool_calls"
    elif stop == "max_tokens":
        finish = "length"
    else:
        finish = "stop"
    return "".join(text_parts).strip(), calls, finish, warnings


def extract_native_openai_chat(data: dict[str, Any], tools: list[ToolSpec], *, max_calls: int | None) -> tuple[str, list[dict[str, Any]], str, list[str]]:
    choices = data.get("choices") or []
    if not choices:
        return "", [], "error", ["no choices in response"]
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    text = (content or "").strip()
    raw_calls: list[dict[str, Any]] = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_calls.append(
            {
                "id": tc.get("id"),
                "tool": fn.get("name"),
                "args": fn.get("arguments") or {},
            }
        )
    calls, warnings = validate_tool_calls(raw_calls, tools, max_calls=max_calls)
    fr = choices[0].get("finish_reason") or "stop"
    if calls or fr == "tool_calls":
        finish = "tool_calls" if calls else "stop"
    elif fr == "length":
        finish = "length"
    else:
        finish = "stop"
    return text, calls, finish, warnings


def extract_native_openai_responses(data: dict[str, Any], tools: list[ToolSpec], *, max_calls: int | None) -> tuple[str, list[dict[str, Any]], str, list[str]]:
    text_parts: list[str] = []
    if isinstance(data.get("output_text"), str):
        text_parts.append(data["output_text"])
    raw_calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype in ("message",):
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    text_parts.append(c.get("text") or "")
        elif itype == "function_call":
            raw_calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "tool": item.get("name"),
                    "args": item.get("arguments") or {},
                }
            )
    if not text_parts and not raw_calls:
        # fallback to older extract shape
        pass
    calls, warnings = validate_tool_calls(raw_calls, tools, max_calls=max_calls)
    status = data.get("status") or ""
    if calls:
        finish = "tool_calls"
    elif status == "incomplete":
        finish = "length"
    else:
        finish = "stop"
    return "".join(text_parts).strip(), calls, finish, warnings


def extract_native_ollama(data: dict[str, Any], tools: list[ToolSpec], *, max_calls: int | None) -> tuple[str, list[dict[str, Any]], str, list[str]]:
    msg = data.get("message") or {}
    text = (msg.get("content") or "").strip()
    raw_calls: list[dict[str, Any]] = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or tc
        raw_calls.append(
            {
                "id": tc.get("id"),
                "tool": fn.get("name"),
                "args": fn.get("arguments") or {},
            }
        )
    calls, warnings = validate_tool_calls(raw_calls, tools, max_calls=max_calls)
    finish = "tool_calls" if calls else "stop"
    return text, calls, finish, warnings


def extract_native(
    style: str,
    data: dict[str, Any],
    tools: list[ToolSpec],
    *,
    max_calls: int | None,
) -> tuple[str, list[dict[str, Any]], str, list[str]]:
    if style == "anthropic":
        return extract_native_anthropic(data, tools, max_calls=max_calls)
    if style == "openai_responses":
        return extract_native_openai_responses(data, tools, max_calls=max_calls)
    if style == "ollama":
        return extract_native_ollama(data, tools, max_calls=max_calls)
    return extract_native_openai_chat(data, tools, max_calls=max_calls)


# --------------------------------------------------------------------------- CLI safety flags when caller tools are present


# Best-effort extras so agent CLIs don't edit the workspace themselves.
# Verified against public docs (Claude Code permissions, Codex sandbox); grok/agy unknown.
DEFAULT_CALLER_TOOLS_EXTRA_ARGS: dict[str, list[str]] = {
    "claude": [
        "--permission-mode",
        "plan",
        "--disallowedTools",
        "Edit,Write,NotebookEdit",
    ],
    "codex": [
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
    ],
}


def cli_extra_args_for_caller_tools(agent_name: str, agent_extra: list[str] | None = None) -> list[str]:
    if agent_extra is not None:
        return list(agent_extra)
    return list(DEFAULT_CALLER_TOOLS_EXTRA_ARGS.get(agent_name, []))
