#!/usr/bin/env python3
"""Tiny stdio MCP server for tests. No network.

Tools:
* ``read_note`` — readOnlyHint true (looper may run it automatically)
* ``write_note`` — not read-only (looper must ask the caller first)

Each ``tools/call`` appends the tool name to ``$FAKE_MCP_LOG`` when that
variable is set, so a test can see what actually ran.
"""

from __future__ import annotations

import json
import os
import sys


def _read() -> dict | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("ascii", "replace")
        if ":" in text:
            key, value = text.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = sys.stdin.buffer.read(length) if length else b""
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _write(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
    sys.stdout.buffer.flush()


def _log(name: str) -> None:
    path = os.environ.get("FAKE_MCP_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(name + "\n")


def main() -> None:
    while True:
        msg = _read()
        if msg is None:
            return
        method = msg.get("method")
        if method == "notifications/initialized":
            continue
        ident = msg.get("id")
        if method == "initialize":
            _write({
                "jsonrpc": "2.0",
                "id": ident,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-notes", "version": "0"},
                },
            })
            continue
        if method == "tools/list":
            _write({
                "jsonrpc": "2.0",
                "id": ident,
                "result": {
                    "tools": [
                        {
                            "name": "read_note",
                            "description": "Read the note",
                            "inputSchema": {"type": "object", "properties": {}},
                            "annotations": {"readOnlyHint": True, "destructiveHint": False},
                        },
                        {
                            "name": "write_note",
                            "description": "Write the note",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                            "annotations": {"readOnlyHint": False, "destructiveHint": False},
                        },
                    ]
                },
            })
            continue
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            _log(str(name))
            if name == "read_note":
                text = "note-body"
            elif name == "write_note":
                text = "wrote:" + str(args.get("text", ""))
            else:
                _write({
                    "jsonrpc": "2.0",
                    "id": ident,
                    "error": {"code": -32602, "message": f"unknown tool {name}"},
                })
                continue
            _write({
                "jsonrpc": "2.0",
                "id": ident,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            })
            continue
        if ident is not None:
            _write({
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": -32601, "message": f"unknown method {method}"},
            })


if __name__ == "__main__":
    main()
