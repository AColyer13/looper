"""Local HTTP API for frontends (``looper serve``).

Binds to localhost by default. Optional bearer token via ``$LOOPER_SERVE_TOKEN``
or ``--token``. Same JSON shapes as the CLI ``--json`` / ``--json-events`` forms.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from looper import api
from looper.config import ConfigError
from looper.events import CancelToken, EventEmitter
from looper.executor import Executor
from looper.policy import Policy, load_policy
from looper.router import route as route_prompt

PolicyFactory = Callable[[], Policy]


class ServeState:
    def __init__(self, policy_factory: PolicyFactory, token: str | None = None):
        self.policy_factory = policy_factory
        self.token = token
        self.runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def policy(self) -> Policy:
        return self.policy_factory()

    def register_run(self, run_id: str, cancel: CancelToken) -> None:
        with self._lock:
            self.runs[run_id] = {"cancel": cancel, "status": "running"}

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock:
            if run_id in self.runs:
                self.runs[run_id]["status"] = status

    def cancel_run(self, run_id: str) -> bool:
        with self._lock:
            info = self.runs.get(run_id)
        if not info:
            return False
        info["cancel"].cancel()
        return True


def _read_json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON body: {exc}") from exc


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: Any) -> None:
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _json_response(handler, status, {"error": message, "ok": False})


def make_handler(state: ServeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
            return

        def _auth(self) -> bool:
            if not state.token:
                return True
            hdr = self.headers.get("Authorization") or ""
            if hdr == f"Bearer {state.token}":
                return True
            _error(self, 401, "unauthorized: set Authorization: Bearer <LOOPER_SERVE_TOKEN>")
            return False

        def _policy_from_body(self, body: dict[str, Any]) -> Policy:
            pol = state.policy()
            team = body.get("team")
            if team is not None:
                pol = api.apply_team_override(pol, team)
            return pol

        def do_GET(self) -> None:  # noqa: N802
            if not self._auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            try:
                if path == "/v1/version":
                    return _json_response(self, 200, api.version_info())
                if path == "/v1/doctor":
                    probe = (qs.get("probe", ["1"])[0] != "0")
                    doc = api.doctor(state.policy(), probe=probe)
                    return _json_response(self, 200 if doc["usable"] else 503, doc)
                if path == "/v1/team":
                    from looper.assign import default_available
                    from looper.tiers import resolve_tier
                    pol = state.policy()
                    avail, missing = default_available(pol)
                    names = pol.team or list(pol.agents)
                    rows = []
                    for n in names:
                        a = pol.agents[n]
                        ti = resolve_tier(a, pol)
                        rows.append({"name": n, "kind": a.kind, "provider": a.provider,
                                     "model": a.model or a.binary, "tier": ti.tier, "size": ti.size,
                                     "ready": n in avail, "reason": missing.get(n, "")})
                    return _json_response(self, 200, {"team": rows, "team_names": pol.team})
                if path == "/v1/models/discover":
                    ollama = (qs.get("ollama_url") or ["http://localhost:11434"])[0]
                    lm = (qs.get("lmstudio_url") or ["http://localhost:1234"])[0]
                    return _json_response(self, 200, {"models": api.discover_models(ollama, lm)})
                if path == "/v1/keys":
                    return _json_response(self, 200, {"keys": api.list_keys(state.policy())})
                return _error(self, 404, f"unknown path {path}")
            except ConfigError as exc:
                return _error(self, 400, str(exc))
            except Exception as exc:  # noqa: BLE001
                return _error(self, 500, f"internal error: {exc}")

        def do_PUT(self) -> None:  # noqa: N802
            if not self._auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                body = _read_json(self)
                if path == "/v1/team":
                    # write team into user config via setup.write_team-like merge is out of scope;
                    # serve keeps an in-process override by rewriting LOOPER via a side file? For
                    # v1 we accept the team for this process by stashing it on state.
                    if not isinstance(body, dict):
                        raise ConfigError("body must be an object with agents/team")
                    # Validate by applying once
                    api.apply_team_override(state.policy(), body)
                    state._team_override = body  # type: ignore[attr-defined]
                    orig = state.policy_factory

                    def factory() -> Policy:
                        return api.apply_team_override(orig(), getattr(state, "_team_override", None))

                    state.policy_factory = factory
                    return _json_response(self, 200, {"ok": True, "team": body.get("team")})
                if path.startswith("/v1/keys/"):
                    provider = path[len("/v1/keys/"):]
                    value = body.get("value") or body.get("key") or ""
                    if not value:
                        raise ConfigError("body.value (API key) is required")
                    result = api.put_key(state.policy(), provider, str(value),
                                         use_keyring=bool(body.get("keyring")))
                    return _json_response(self, 200, result)
                return _error(self, 404, f"unknown path {path}")
            except ConfigError as exc:
                return _error(self, 400, str(exc))
            except Exception as exc:  # noqa: BLE001
                return _error(self, 500, f"internal error: {exc}")

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                body = _read_json(self) if not path.endswith("/cancel") else (_read_json(self) or {})
                if path == "/v1/plan":
                    return self._plan(body)
                if path == "/v1/ask":
                    return self._ask(body)
                if path == "/v1/run":
                    return self._run(body)
                if path.startswith("/v1/runs/") and path.endswith("/cancel"):
                    run_id = path[len("/v1/runs/"): -len("/cancel")]
                    ok = state.cancel_run(run_id)
                    return _json_response(self, 200 if ok else 404,
                                          {"ok": ok, "run_id": run_id,
                                           "error": None if ok else "unknown run_id"})
                return _error(self, 404, f"unknown path {path}")
            except ConfigError as exc:
                return _error(self, 400, str(exc))
            except Exception as exc:  # noqa: BLE001
                return _error(self, 500, f"internal error: {exc}")

        def _plan(self, body: dict[str, Any]) -> None:
            task = (body.get("task") or body.get("prompt") or "").strip()
            if not task:
                raise ConfigError("task is required")
            pol = self._policy_from_body(body)
            assign = {str(k): str(v) for k, v in (body.get("assign") or {}).items()}
            labels = {str(k): str(v) for k, v in (body.get("role_labels") or {}).items()}
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            warnings = api.validate_assign(pol, assign) if assign else []
            decision = route_prompt(task, pol, override=body.get("loop"))
            ex = Executor(pol, force_agent=body.get("agent"), assign=assign or None,
                          max_iterations=body.get("max_iterations"), cwd=body.get("cwd"))
            plan = api.enrich_plan(ex.plan(task, decision.loop, decision),
                                   role_labels=labels, meta=meta, assign=assign,
                                   assign_warnings=warnings)
            _json_response(self, 200, plan)

        def _ask(self, body: dict[str, Any]) -> None:
            agent = body.get("agent")
            if not agent:
                raise ConfigError("agent is required")
            prompt = body.get("prompt") or body.get("task")
            has_conv = bool(prompt or body.get("messages") or body.get("tool_results"))
            if not has_conv:
                raise ConfigError("prompt, messages, or tool_results is required")
            pol = self._policy_from_body(body)
            # Pass the full body as request so tools/messages/tool_results flow through.
            result = api.ask(
                pol, str(agent),
                prompt=(str(prompt).strip() if isinstance(prompt, str) and prompt.strip() else None),
                request=body,
                cwd=body.get("cwd"),
                timeout=body.get("timeout"),
                max_tokens=body.get("max_tokens"),
                temperature=body.get("temperature"),
            )
            status = 200 if result["ok"] else 502
            _json_response(self, status, result)

        def _run(self, body: dict[str, Any]) -> None:
            task = (body.get("task") or body.get("prompt") or "").strip()
            if not task:
                raise ConfigError("task is required")
            pol = self._policy_from_body(body)
            assign = {str(k): str(v) for k, v in (body.get("assign") or {}).items()}
            labels = {str(k): str(v) for k, v in (body.get("role_labels") or {}).items()}
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            if assign:
                api.validate_assign(pol, assign)  # raise on unknown agents
            decision = route_prompt(task, pol, override=body.get("loop"))
            run_id = str(uuid.uuid4())
            cancel = CancelToken()
            state.register_run(run_id, cancel)

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Looper-Run-Id", run_id)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            class _Chunked:
                def __init__(self, wfile):
                    self.wfile = wfile

                def write(self, s: str) -> None:
                    data = s.encode("utf-8")
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()

                def flush(self) -> None:
                    self.wfile.flush()

                def close(self) -> None:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()

            stream = _Chunked(self.wfile)
            meta = {**meta, "run_id": run_id}
            emitter = EventEmitter(stream, meta=meta, role_labels=labels)  # type: ignore[arg-type]
            try:
                ex = Executor(pol, force_agent=body.get("agent"), assign=assign or None,
                              max_iterations=body.get("max_iterations"), cwd=body.get("cwd"),
                              cancel=cancel, emitter=emitter, meta=meta, role_labels=labels)
                result = ex.run(task, decision.loop, decision,
                                write_transcript=not body.get("no_transcript", False))
                state.finish_run(run_id, result.status)
            except Exception as exc:  # noqa: BLE001
                emitter.emit("error", message=str(exc), status="failed")
                emitter.emit("run_finished", status="failed", stop_reason=str(exc),
                             final_output="", transcript_path=None)
                state.finish_run(run_id, "failed")
            finally:
                stream.close()

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, *, config_path=None,
          token: str | None = None) -> ThreadingHTTPServer:
    """Start the server (blocking). Returns the server after shutdown."""
    token = token if token is not None else os.environ.get("LOOPER_SERVE_TOKEN") or None

    def factory() -> Policy:
        return load_policy(config_path)

    # validate config eagerly
    factory()
    state = ServeState(factory, token=token)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    return httpd
