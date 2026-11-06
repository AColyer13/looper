"""Minimal MCP client: stdio (Content-Length framing) and streamable HTTP.

SSE responses are accepted on the HTTP transport. No network is required for
stdio. Sessions live for one turn and are closed by the caller.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from looper import __version__
from looper.mcp_config import ServerSpec, scrub

_MAX_BODY = 8_000_000
_PROTOCOL = "2024-11-05"


class McpError(RuntimeError):
    """A server failed to speak MCP. The message is scrubbed of credentials."""


@dataclass
class McpToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def exposed_name(self) -> str:
        from looper.mcp_config import expose_tool
        return expose_tool(self.server, self.name)


def parse_sse_body(raw: bytes) -> dict[str, Any]:
    """Return the last JSON-RPC object carried in an SSE body."""
    found: dict[str, Any] | None = None
    text = raw.decode("utf-8", "replace")
    for block in text.split("\n\n"):
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            found = obj
    if found is None:
        raise McpError("SSE response contained no JSON-RPC message")
    return found


def _read_frame(stream: Any) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("ascii", "replace")
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise McpError("MCP frame has a bad Content-Length") from exc
    if length < 0 or length > _MAX_BODY:
        raise McpError("MCP frame Content-Length is out of range")
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise McpError("MCP server closed the stream mid-frame")
        body += chunk
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise McpError(f"MCP frame is not JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise McpError("MCP frame must be a JSON object")
    return data


def _write_frame(stream: Any, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
    stream.flush()


def _read_frame_timeout(stream: Any, timeout: float, proc: subprocess.Popen[bytes] | None) -> dict[str, Any] | None:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["msg"] = _read_frame(stream)
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        if proc is not None and proc.poll() is None:
            proc.kill()
        thread.join(1.0)
        raise McpError(f"MCP server timed out after {timeout:g}s")
    if "err" in box:
        err = box["err"]
        if isinstance(err, McpError):
            raise err
        raise McpError(str(err)) from err
    return box.get("msg")


class _RpcSession:
    def __init__(self, server: ServerSpec, timeout: float):
        self.server = server
        self.timeout = timeout
        self._next = 0

    def _fail(self, message: str) -> McpError:
        return McpError(scrub(message, self.server.secrets))

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def initialize(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": _PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "looper", "version": __version__},
        })
        if not isinstance(result, dict):
            raise self._fail("initialize returned no result")
        self.notify("notifications/initialized", {})

    def list_tools(self) -> list[McpToolInfo]:
        tools: list[McpToolInfo] = []
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = self.request("tools/list", params)
            if not isinstance(result, dict):
                raise self._fail("tools/list returned no result")
            for item in result.get("tools") or []:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                schema = item.get("inputSchema") or {"type": "object", "properties": {}}
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                annotations = item.get("annotations") or {}
                if not isinstance(annotations, dict):
                    annotations = {}
                tools.append(McpToolInfo(
                    server=self.server.name,
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    input_schema=schema,
                    annotations=annotations,
                ))
            cursor = result.get("nextCursor") or None
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return True, "" if result is None else str(result)
        parts: list[str] = []
        for block in result.get("content") or []:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text") or ""))
        text = "\n".join(p for p in parts if p) if parts else json.dumps(result, default=str)
        if result.get("isError"):
            return False, text or "MCP tool returned an error"
        return True, text


class StdioSession(_RpcSession):
    def __init__(self, server: ServerSpec, timeout: float):
        super().__init__(server, timeout)
        if server.error:
            raise McpError(server.error)
        if not server.command:
            raise McpError(f"MCP server {server.name} has no command")
        env = {**os.environ, **server.env}
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self.proc = subprocess.Popen(
                [server.command, *server.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise self._fail(f"failed to start MCP server {server.name}: {exc}") from exc
        self.initialize()

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise self._fail(f"MCP server {self.server.name} has no stdin")
        try:
            _write_frame(self.proc.stdin, payload)
        except OSError as exc:
            raise self._fail(f"MCP server {self.server.name} write failed: {exc}") from exc

    def _recv_matching(self, expect: int | None) -> dict[str, Any]:
        while True:
            msg = _read_frame_timeout(self.proc.stdout, self.timeout, self.proc)
            if msg is None:
                err = ""
                if self.proc.poll() is not None and self.proc.stderr is not None:
                    try:
                        err = self.proc.stderr.read().decode("utf-8", "replace")[:400]
                    except Exception:  # noqa: BLE001
                        err = ""
                detail = f": {err.strip()}" if err.strip() else ""
                raise self._fail(f"MCP server {self.server.name} closed{detail}")
            if expect is None:
                return msg
            if msg.get("id") == expect:
                return msg
            # Server notification or a response for a different id: ignore.

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        msg = self._recv_matching(ident)
        if "error" in msg and msg["error"]:
            err = msg["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            raise self._fail(f"MCP {method} failed: {text}")
        return msg.get("result") or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=1)
        except Exception:  # noqa: BLE001
            pass
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:  # noqa: BLE001
                    pass


class HttpSession(_RpcSession):
    """Streamable HTTP. A JSON body or an SSE body are both accepted."""

    def __init__(self, server: ServerSpec, timeout: float):
        super().__init__(server, timeout)
        if server.error:
            raise McpError(server.error)
        if not server.url:
            raise McpError(f"MCP server {server.name} has no url")
        self.session_id: str | None = None
        self.initialize()

    def _post(self, payload: dict[str, Any], *, expect_body: bool) -> dict[str, Any] | None:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(self.server.headers or {}),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.server.url or "", data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            raise self._fail(
                f"MCP HTTP {exc.code}: {raw.decode('utf-8', 'replace')[:300]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise self._fail(f"MCP HTTP request failed: {exc}") from exc
        if not expect_body or status == 202 or not raw:
            return None
        try:
            if "text/event-stream" in ctype:
                return parse_sse_body(raw)
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, McpError) as exc:
            raise self._fail(f"MCP HTTP response was not JSON-RPC: {exc}") from exc
        if not isinstance(parsed, dict):
            raise self._fail("MCP HTTP response must be a JSON object")
        return parsed

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        msg = self._post(
            {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}},
            expect_body=True,
        )
        if not msg:
            raise self._fail(f"MCP {method} returned an empty body")
        if "error" in msg and msg["error"]:
            err = msg["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            raise self._fail(f"MCP {method} failed: {text}")
        return msg.get("result") or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._post(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            expect_body=False,
        )

    def close(self) -> None:
        return None


class McpHub:
    """One short-lived session per server, reused for list + calls in a turn."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._sessions: dict[str, _RpcSession] = {}

    def _open(self, server: ServerSpec) -> _RpcSession:
        existing = self._sessions.get(server.name)
        if existing is not None:
            return existing
        if server.error:
            raise McpError(server.error)
        if server.transport == "stdio":
            session: _RpcSession = StdioSession(server, self.timeout)
        else:
            session = HttpSession(server, self.timeout)
        self._sessions[server.name] = session
        return session

    def list_tools(self, server: ServerSpec) -> list[McpToolInfo]:
        return self._open(server).list_tools()

    def call_tool(self, server: ServerSpec, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        return self._open(server).call_tool(name, arguments)

    def close(self) -> None:
        for session in self._sessions.values():
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()


def connect_and_list(server: ServerSpec, timeout: float = 3.0) -> list[McpToolInfo]:
    hub = McpHub(timeout=timeout)
    try:
        return hub.list_tools(server)
    finally:
        hub.close()
