"""One stateless ask turn: looper-owned tools, then hand the rest back.

looper executes ``load_skill`` and MCP tools whose policy says ``auto``.
Caller-owned tools are always returned unchanged. MCP tools that need a human
decision are returned as ``pending`` with ``finish_reason: approval_required``.
The caller resumes by sending the same ``messages`` plus ``approvals``.
"""

from __future__ import annotations

from typing import Any

from looper.config import ConfigError
from looper.mcp_client import McpError, McpHub, McpToolInfo
from looper.mcp_config import (
    McpSettings,
    RolePolicy,
    decide,
    parse_exposed_tool,
    scrub,
    tool_visible,
)
from looper.tools import ToolSpec, apply_tool_results
from looper.workspace import Workspace, load_skill_result

MAX_LOOPER_HOPS = 4


def persona_supplied(system: str | None, messages: Any) -> bool:
    if isinstance(system, str) and system.strip():
        return True
    if isinstance(messages, list):
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "system"
                and str(message.get("content") or "").strip()
            ):
                return True
    return False


def merge_system(base: str | None, extra: str | None) -> str | None:
    base_text = (base or "").strip()
    extra_text = (extra or "").strip()
    if not extra_text:
        return base_text or None
    if not base_text:
        return extra_text
    return base_text + "\n\n" + extra_text


def mcp_tool_spec(info: McpToolInfo) -> ToolSpec:
    schema = info.input_schema or {"type": "object", "properties": {}}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": schema.get("properties") or {}}
    return ToolSpec(
        name=info.exposed_name,
        description=info.description or info.name,
        parameters=schema,
    )


def visible_mcp_tools(ws: Workspace, infos: list[McpToolInfo]) -> list[McpToolInfo]:
    by_server = ws.mcp.servers
    out: list[McpToolInfo] = []
    for info in infos:
        server = by_server.get(info.server)
        if server is None or server.error:
            continue
        if tool_visible(server, info.name):
            out.append(info)
    return out


def _role_policy(ws: Workspace, role: str | None) -> RolePolicy | None:
    doc = ws.role_doc(role) if role else None
    return doc.policy if doc else None


def classify_calls(
    calls: list[dict[str, Any]],
    *,
    caller_names: set[str],
    owned_names: set[str],
    mcp_by_name: dict[str, McpToolInfo],
    settings: McpSettings,
    ws: Workspace,
    role: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into caller, auto, pending, denied.

    A name the caller defined is always caller-owned, even if it matches a
    looper tool. That keeps today's caller tools on the response.
    """
    policy = _role_policy(ws, role)
    caller: list[dict[str, Any]] = []
    auto: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    for call in calls:
        name = str(call.get("tool") or "")
        if name not in owned_names or name in caller_names:
            caller.append(call)
            continue
        if name == "load_skill":
            auto.append(call)
            continue
        info = mcp_by_name.get(name)
        parsed = parse_exposed_tool(name)
        if info is None or parsed is None:
            denied.append({**call, "_reason": "unknown"})
            continue
        server = settings.servers.get(parsed[0])
        if server is None:
            denied.append({**call, "_reason": "unknown"})
            continue
        action, reason = decide(settings, server, parsed[1], info.annotations, policy)
        annotated = {**call, "_reason": reason, "_server": server.name, "_mcp_name": parsed[1]}
        if action == "auto":
            auto.append(annotated)
        elif action == "deny":
            denied.append(annotated)
        else:
            pending.append(annotated)
    return caller, auto, pending, denied


def pending_payload(call: dict[str, Any], info: McpToolInfo | None) -> dict[str, Any]:
    annotations = info.annotations if info else {}
    hints = {
        "readOnlyHint": annotations.get("readOnlyHint"),
        "destructiveHint": annotations.get("destructiveHint"),
    }
    if annotations.get("title"):
        hints["title"] = annotations.get("title")
    return {
        "id": call.get("id"),
        "tool": call.get("tool"),
        "server": call.get("_server"),
        "name": call.get("_mcp_name"),
        "args": call.get("args") or {},
        "reason": call.get("_reason") or "not_read_only",
        "annotations": hints,
    }


def _result(call: dict[str, Any], ok: bool, output: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": call.get("id"),
        "tool": call.get("tool"),
        "ok": ok,
        "output": output,
    }
    if not ok:
        entry["error"] = output
    return entry


def execute_owned(
    call: dict[str, Any],
    *,
    ws: Workspace,
    hub: McpHub,
) -> dict[str, Any]:
    name = str(call.get("tool") or "")
    if name == "load_skill":
        ok, text = load_skill_result(ws, call.get("args") or {})
        return _result(call, ok, text)
    parsed = parse_exposed_tool(name)
    if parsed is None:
        return _result(call, False, f"unknown looper tool {name}")
    server = ws.mcp.servers.get(parsed[0])
    if server is None or server.error:
        return _result(call, False, server.error if server and server.error else f"unknown MCP server {parsed[0]}")
    reason = call.get("_reason")
    if reason in ("deny", "deny_all", "unknown"):
        return _result(call, False, f"denied by policy ({reason})")
    try:
        ok, text = hub.call_tool(server, parsed[1], call.get("args") or {})
        return _result(call, ok, scrub(text, server.secrets))
    except McpError as exc:
        return _result(call, False, scrub(str(exc), server.secrets))


def apply_approvals(
    messages: list[dict[str, Any]],
    approvals: Any,
    *,
    ws: Workspace,
    hub: McpHub,
    role: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Execute approved MCP calls found in ``messages``. Stateless: args come from the transcript."""
    if approvals is None:
        return messages, []
    if not isinstance(approvals, list):
        raise ConfigError("approvals must be an array")
    if not approvals:
        return messages, []
    by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                by_id[str(call["id"])] = call
    answered = {
        str(message.get("tool_call_id"))
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    policy = _role_policy(ws, role)
    warnings: list[str] = []
    results: list[dict[str, Any]] = []
    for index, item in enumerate(approvals):
        if not isinstance(item, dict):
            raise ConfigError(f"approvals[{index}] must be an object")
        cid = item.get("id") or item.get("tool_call_id")
        if not cid:
            raise ConfigError(f"approvals[{index}].id is required")
        cid = str(cid)
        decision = str(item.get("decision") if item.get("decision") is not None else "").strip().lower()
        if decision in ("approve", "approved", "yes", "true"):
            decision = "approve"
        elif decision in ("deny", "denied", "no", "false"):
            decision = "deny"
        else:
            raise ConfigError(f"approvals[{index}].decision must be 'approve' or 'deny'")
        if cid in answered:
            continue
        call = by_id.get(cid)
        if call is None:
            raise ConfigError(f"approvals: unknown tool call id {cid}")
        parsed = parse_exposed_tool(str(call.get("tool") or ""))
        if parsed is None:
            raise ConfigError(
                f"approvals: {cid} refers to {call.get('tool')!r}, which is not an MCP tool. "
                "Caller tools resume through tool_results."
            )
        server_name, tool_name = parsed
        if decision == "deny":
            reason = str(item.get("reason") or "Denied by caller")
            results.append(_result(call, False, reason))
            continue
        server = ws.mcp.servers.get(server_name)
        if server is None or server.error:
            message = server.error if server and server.error else f"unknown MCP server {server_name}"
            results.append(_result(call, False, message))
            continue
        action, why = decide(ws.mcp, server, tool_name, {}, policy)
        # The caller already approved. Honor deny (it outranks approval) and run otherwise.
        if action == "deny":
            results.append(_result(call, False, f"denied by policy ({why})"))
            warnings.append(f"approval for {call.get('tool')} refused by policy ({why})")
            continue
        try:
            ok, text = hub.call_tool(server, tool_name, call.get("args") or {})
            results.append(_result(call, ok, scrub(text, server.secrets)))
        except McpError as exc:
            results.append(_result(call, False, scrub(str(exc), server.secrets)))
    if not results:
        return messages, warnings
    return apply_tool_results(messages, results), warnings
