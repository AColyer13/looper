"""Minimal HTTP runner for kind=api/local agents (stdlib only, no SDKs).

Styles:
* ``openai``           POST {base}/chat/completions   (xAI, Gemini compat, OpenRouter, LM Studio, vLLM, llama.cpp)
* ``openai_responses`` POST {base}/responses          (OpenAI first-party)
* ``anthropic``        POST {base}/messages           (Anthropic native Messages API)
* ``ollama``           POST {base}/api/chat           (Ollama native: lets us set num_ctx / num_predict / think)

The transport is injectable so tests never touch the network. Keys are read at
call time via ``looper.credentials.get_key`` and are scrubbed from any error text.
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


def build_request(inv: Invocation, key: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
    a = inv.agent
    base = (a.base_url or "").rstrip("/")
    style = a.api_style or "openai"
    msgs = [{"role": "user", "content": inv.prompt}]
    headers: dict[str, str] = {}
    if style == "anthropic":
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
        body: dict[str, Any] = {"model": a.model, "max_tokens": inv.max_tokens or 8192, "messages": msgs}
        if inv.temperature is not None:
            body["temperature"] = inv.temperature
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
        body = {"model": a.model, "messages": msgs, "stream": False, "options": opts}
        if inv.disable_thinking:
            body["think"] = False
        return f"{base}/api/chat", headers, body
    if style == "openai_responses":
        body = {"model": a.model, "input": inv.prompt}
        if inv.max_tokens:
            body["max_output_tokens"] = inv.max_tokens
        if inv.temperature is not None:
            body["temperature"] = inv.temperature
        return f"{base}/responses", headers, body
    body = {"model": a.model, "messages": msgs}
    if inv.max_tokens:
        body["max_tokens"] = inv.max_tokens
    if inv.temperature is not None:
        body["temperature"] = inv.temperature
    return f"{base}/chat/completions", headers, body


def extract_text(style: str, data: Any) -> str:
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
        if not key and a.api_key_env and not a.key_optional:
            return CallResult(a.name, pseudo, False, "",
                              error=f"missing API key ${a.api_key_env} (run `looper keys set {a.provider}`)")
        url, headers, body = build_request(inv, key)
        t0 = time.monotonic()

        def scrub(text: str) -> str:
            return text.replace(key, "••••") if key else text

        try:
            status, data = self.transport("POST", url, headers, body, inv.timeout)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            timed_out = isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in str(exc)
            return CallResult(a.name, pseudo, False, "", error=scrub(f"{type(exc).__name__}: {exc}"),
                              timed_out=timed_out, seconds=time.monotonic() - t0)
        secs = time.monotonic() - t0
        if status >= 400:
            return CallResult(a.name, pseudo, False, "", returncode=status,
                              error=scrub(f"HTTP {status}: {_error_text(data)}"), seconds=secs)
        try:
            text = extract_text(a.api_style or "openai", data)
        except (ValueError, AttributeError, TypeError) as exc:
            return CallResult(a.name, pseudo, False, "", returncode=status, error=f"bad response: {exc}", seconds=secs)
        if not text.strip():
            return CallResult(a.name, pseudo, False, "", returncode=status, seconds=secs,
                              error="empty response (thinking models can spend the whole output budget; "
                                    "raise max_output_tokens)")
        return CallResult(a.name, pseudo, True, text.strip(), returncode=status, seconds=secs)
