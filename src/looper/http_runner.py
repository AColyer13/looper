"""Minimal HTTP runner for kind=api/local agents (stdlib only, no SDKs).

Styles:
* ``openai``           POST {base}/chat/completions   (xAI, Gemini compat, OpenRouter, LM Studio, vLLM, llama.cpp)
* ``openai_responses`` POST {base}/responses          (OpenAI first-party)
* ``anthropic``        POST {base}/messages           (Anthropic native Messages API)
* ``ollama``           POST {base}/api/chat           (Ollama native: lets us set num_ctx / num_predict / think)

Supports native function/tool calling when ``Invocation.protocol == "native"`` and
tools are present. The transport is injectable so tests never touch the network.
Keys are read at call time via ``looper.credentials.get_key`` and are scrubbed
from any error text.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from looper.credentials import get_key
from looper.runners import CallResult, Invocation
from looper import tools as toolmod

# transport(method, url, headers, body|None, timeout) -> (status, parsed json | text)
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None, float], tuple[int, Any]]

ANTHROPIC_VERSION = "2023-06-01"


def urllib_transport(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None,
                     timeout: float) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured endpoint
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else str(exc)
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _normalize_tools(raw: list[Any]) -> list[toolmod.ToolSpec]:
    if not raw:
        return []
    if isinstance(raw[0], toolmod.ToolSpec):
        return list(raw)  # type: ignore[arg-type]
    return toolmod.parse_tools(raw)


def _messages_for(inv: Invocation) -> list[dict[str, Any]]:
    if inv.messages:
        return list(inv.messages)
    msgs: list[dict[str, Any]] = []
    if inv.system:
        msgs.append({"role": "system", "content": inv.system})
    msgs.append({"role": "user", "content": inv.prompt})
    return msgs


def build_request(inv: Invocation, key: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
    a = inv.agent
    base = (a.base_url or "").rstrip("/")
    style = a.api_style or "openai"
    tools = _normalize_tools(list(inv.tools or []))
    use_native = inv.protocol == "native" and bool(tools)
    messages = _messages_for(inv)
    headers: dict[str, str] = {}

    if style == "anthropic":
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
        system, anthro_msgs = toolmod.messages_to_anthropic(messages)
        body: dict[str, Any] = {
            "model": a.model,
            "max_tokens": inv.max_tokens or 8192,
            "messages": anthro_msgs if anthro_msgs else [{"role": "user", "content": inv.prompt}],
        }
        if system:
            body["system"] = system
        if inv.temperature is not None:
            body["temperature"] = inv.temperature
        if use_native:
            body["tools"] = toolmod.tools_for_anthropic(tools)
        return f"{base}/messages", headers, body

    if key:
        headers["Authorization"] = f"Bearer {key}"

    if style == "ollama":
        opts: dict[str, Any] = {}
        if inv.num_ctx:
            opts["num_ctx"] = inv.num_ctx
        if inv.max_tokens:
            opts["num_predict"] = inv.max_tokens
        if inv.temperature is not None:
            opts["temperature"] = inv.temperature
        ollama_msgs = toolmod.messages_to_ollama(messages)
        body = {"model": a.model, "messages": ollama_msgs, "stream": False, "options": opts}
        if inv.disable_thinking:
            body["think"] = False
        if use_native:
            body["tools"] = toolmod.tools_for_ollama(tools)
        return f"{base}/api/chat", headers, body

    if style == "openai_responses":
        if use_native or len(messages) > 1 or (messages and messages[0].get("role") == "system"):
            inp: Any = toolmod.messages_to_openai_responses(messages)
        else:
            inp = inv.prompt
        body = {"model": a.model, "input": inp}
        if inv.max_tokens:
            body["max_output_tokens"] = inv.max_tokens
        if inv.temperature is not None:
            body["temperature"] = inv.temperature
        if use_native:
            body["tools"] = toolmod.tools_for_openai_responses(tools)
        return f"{base}/responses", headers, body

    # OpenAI-compatible chat completions
    chat_msgs = toolmod.messages_to_openai_chat(messages)
    body = {"model": a.model, "messages": chat_msgs}
    if inv.max_tokens:
        body["max_tokens"] = inv.max_tokens
    if inv.temperature is not None:
        body["temperature"] = inv.temperature
    if use_native:
        body["tools"] = toolmod.tools_for_openai_chat(tools)
    return f"{base}/chat/completions", headers, body


def extract_text(style: str, data: Any) -> str:
    """Plain-text extract (no tools). Kept for backwards-compatible call sites."""
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    if style == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if style == "ollama":
        return (data.get("message") or {}).get("content", "")
    if style == "openai_responses":
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        parts = []
        for item in data.get("output", []) or []:
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    parts.append(c.get("text", ""))
        return "".join(parts)
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("no choices in response")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content or ""


def _error_text(data: Any) -> str:
    if isinstance(data, dict):
        e = data.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e)
        if e:
            return str(e)
    return str(data)[:300]


class HttpRunner:
    def __init__(self, transport: Transport | None = None, key_lookup: Callable[[str | None], str | None] = get_key):
        self.transport = transport or urllib_transport
        self.key_lookup = key_lookup

    def __call__(self, inv: Invocation) -> CallResult:
        a = inv.agent
        key = self.key_lookup(a.api_key_env)
        pseudo = [f"{a.api_style}:{a.base_url}", f"model={a.model}"]
        protocol = inv.protocol or "none"
        tools = _normalize_tools(list(inv.tools or []))
        if not key and a.api_key_env and not a.key_optional:
            return CallResult(
                a.name, pseudo, False, "",
                error=f"missing API key ${a.api_key_env} (run `looper keys set {a.provider}`)",
                protocol=protocol, finish_reason="error",
            )
        url, headers, body = build_request(inv, key)
        t0 = time.monotonic()

        def scrub(text: str) -> str:
            return text.replace(key, "••••") if key else text

        try:
            status, data = self.transport("POST", url, headers, body, inv.timeout)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            timed_out = isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in str(exc)
            return CallResult(
                a.name, pseudo, False, "", error=scrub(f"{type(exc).__name__}: {exc}"),
                timed_out=timed_out, seconds=time.monotonic() - t0,
                protocol=protocol, finish_reason="error",
            )
        secs = time.monotonic() - t0
        if status >= 400:
            return CallResult(
                a.name, pseudo, False, "", returncode=status,
                error=scrub(f"HTTP {status}: {_error_text(data)}"), seconds=secs,
                protocol=protocol, finish_reason="error",
            )
        if not isinstance(data, dict):
            return CallResult(
                a.name, pseudo, False, "", returncode=status,
                error="bad response: not a JSON object", seconds=secs,
                protocol=protocol, finish_reason="error",
            )

        warnings: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        finish = "stop"
        try:
            if protocol == "native" and tools:
                text, tool_calls, finish, warnings = toolmod.extract_native(
                    a.api_style or "openai", data, tools, max_calls=inv.max_tool_calls,
                )
            else:
                text = extract_text(a.api_style or "openai", data)
                if protocol == "json_block" and tools:
                    text, tool_calls, w = toolmod.parse_json_block(
                        text, tools, max_calls=inv.max_tool_calls,
                    )
                    warnings.extend(w)
                    if tool_calls:
                        finish = "tool_calls"
        except (ValueError, AttributeError, TypeError) as exc:
            return CallResult(
                a.name, pseudo, False, "", returncode=status, error=f"bad response: {exc}",
                seconds=secs, protocol=protocol, finish_reason="error",
            )

        # Native tool_calls with empty text is a valid "I need a tool" turn.
        if not text.strip() and not tool_calls:
            return CallResult(
                a.name, pseudo, False, "", returncode=status, seconds=secs,
                error="empty response (thinking models can spend the whole output budget; "
                      "raise max_output_tokens)",
                protocol=protocol, finish_reason="error", warnings=warnings,
            )
        return CallResult(
            a.name, pseudo, True, text.strip(), returncode=status, seconds=secs,
            tool_calls=tool_calls, finish_reason=finish, protocol=protocol, warnings=warnings,
        )
