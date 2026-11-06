"""Discover local models and their size / context metadata.

* Ollama     GET /api/tags, POST /api/show  -> general.parameter_count, <arch>.context_length,
                                               details.parameter_size, details.quantization_level,
                                               <arch>.expert_count / expert_used_count (MoE)
* LM Studio  GET /api/v1/models (params_string, max_context_length, quantization.name)
             fallback GET /api/v0/models (arch, quantization, max_context_length)
* generic    GET /v1/models  vLLM: max_model_len; llama.cpp: meta.n_params, meta.n_ctx_train

Everything is best-effort with short timeouts; unreachable servers just return [].
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from looper.http_runner import Transport, urllib_transport
from looper.tiers import parse_params, parse_size_string

OLLAMA_URL = "http://localhost:11434"
LMSTUDIO_URL = "http://localhost:1234"


@dataclass
class LocalModel:
    provider: str  # ollama | lmstudio | openai_compatible
    model: str
    base_url: str
    params_b: float | None = None
    active_params_b: float | None = None
    context_window: int | None = None
    quantization: str | None = None
    family: str | None = None
    source: str = "list"

    def to_dict(self) -> dict:
        return asdict(self)


def _get(t: Transport, url: str, timeout: float) -> Any:
    try:
        status, data = t("GET", url, {}, None, timeout)
    except Exception:  # noqa: BLE001 - discovery is best effort
        return None
    return data if status < 400 and isinstance(data, dict) else None


def parse_ollama_show(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Extract params/active/context/quant from an /api/show payload."""
    info = data.get("model_info") or {}
    details = data.get("details") or {}
    arch = info.get("general.architecture") or details.get("family") or ""
    total = info.get("general.parameter_count")
    total_b = float(total) / 1e9 if isinstance(total, (int, float)) and total else parse_size_string(details.get("parameter_size"))
    ctx = info.get(f"{arch}.context_length") if arch else None
    if ctx is None:
        ctx = next((v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)), None)
    active_b = None
    experts = info.get(f"{arch}.expert_count")
    if experts:  # MoE: Ollama doesn't report active params; take them from the name if present
        named = parse_params(name)
        if named.active_b:
            active_b = named.active_b
    return {"params_b": round(total_b, 3) if total_b else None, "active_params_b": active_b,
            "context_window": int(ctx) if ctx else None, "quantization": details.get("quantization_level"),
            "family": details.get("family") or arch or None}


def ollama_models(base: str = OLLAMA_URL, *, transport: Transport | None = None, show: bool = True,
                  timeout: float = 2.0) -> list[LocalModel]:
    t = transport or urllib_transport
    data = _get(t, f"{base.rstrip('/')}/api/tags", timeout)
    if not data:
        return []
    out = []
    for m in data.get("models", []) or []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        d = m.get("details") or {}
        lm = LocalModel("ollama", name, base, params_b=parse_size_string(d.get("parameter_size")),
                        quantization=d.get("quantization_level"), family=d.get("family"))
        named = parse_params(name)
        lm.active_params_b = named.active_b
        if show:
            try:
                status, sd = t("POST", f"{base.rstrip('/')}/api/show", {}, {"model": name}, timeout)
                if status < 400 and isinstance(sd, dict):
                    meta = parse_ollama_show(name, sd)
                    for k, v in meta.items():
                        if v is not None:
                            setattr(lm, k, v)
                    lm.source = "api/show"
            except Exception:  # noqa: BLE001
                pass
        out.append(lm)
    return out


def lmstudio_models(base: str = LMSTUDIO_URL, *, transport: Transport | None = None,
                    timeout: float = 2.0) -> list[LocalModel]:
    t = transport or urllib_transport
    root = base.rstrip("/").removesuffix("/v1")
    api_base = f"{root}/v1"
    data = _get(t, f"{root}/api/v1/models", timeout)
    out: list[LocalModel] = []
    if data and isinstance(data.get("models"), list):
        for m in data["models"]:
            if m.get("type") not in (None, "llm", "vlm"):
                continue
            key = m.get("key") or m.get("id")
            q = m.get("quantization")
            named = parse_params(key or "")
            out.append(LocalModel("lmstudio", key, api_base,
                                  params_b=parse_size_string(m.get("params_string")) or named.total_b,
                                  active_params_b=named.active_b, context_window=m.get("max_context_length"),
                                  quantization=q.get("name") if isinstance(q, dict) else q,
                                  family=m.get("architecture"), source="api/v1/models"))
        return out
    data = _get(t, f"{root}/api/v0/models", timeout)
    for m in (data or {}).get("data", []) or []:
        if m.get("type") not in (None, "llm", "vlm"):
            continue
        named = parse_params(m.get("id", ""))
        out.append(LocalModel("lmstudio", m["id"], api_base, params_b=named.total_b, active_params_b=named.active_b,
                              context_window=m.get("max_context_length"), quantization=m.get("quantization"),
                              family=m.get("arch"), source="api/v0/models"))
    return out


def openai_compatible_models(base: str, *, transport: Transport | None = None, timeout: float = 2.0,
                             provider: str = "openai_compatible") -> list[LocalModel]:
    t = transport or urllib_transport
    data = _get(t, f"{base.rstrip('/')}/models", timeout)
    out = []
    for m in (data or {}).get("data", []) or []:
        mid = m.get("id")
        if not mid:
            continue
        meta = m.get("meta") or {}
        named = parse_params(mid)
        n_params = meta.get("n_params")
        out.append(LocalModel(provider, mid, base,
                              params_b=(float(n_params) / 1e9 if n_params else named.total_b),
                              active_params_b=named.active_b,
                              context_window=m.get("max_model_len") or meta.get("n_ctx_train"),
                              source="v1/models"))
    return out


def discover_all(*, transport: Transport | None = None, ollama_url: str = OLLAMA_URL,
                 lmstudio_url: str = LMSTUDIO_URL, timeout: float = 1.5) -> list[LocalModel]:
    return ollama_models(ollama_url, transport=transport, timeout=timeout) + \
        lmstudio_models(lmstudio_url, transport=transport, timeout=timeout)
