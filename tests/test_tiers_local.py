import pytest

from looper.local import lmstudio_models, ollama_models, openai_compatible_models, parse_ollama_show
from looper.policy import AgentSpec
from looper.tiers import parse_params, parse_size_string, resolve_tier


@pytest.mark.parametrize("name,total,active", [
    ("qwen3:8b", 8, None), ("qwen3.5:0.8b", 0.8, None), ("Qwen3.5-35B-A3B", 35, 3), ("qwen3:30b-a3b", 30, 3),
    ("gemma3:270m", 0.27, None), ("mixtral:8x7b", 56, 14), ("gemma4:e4b", 4, None),
    ("llama3.1:8b-instruct-q4_K_M", 8, None), ("gpt-oss:20b", 21, 3.6), ("phi4-mini:3.8b", 3.8, None),
    ("qwen3.8-2.4t-a95b", 2400, 95), ("Qwen3.5-122B-A10B", 122, 10), ("qwen2.5-coder-7b-instruct", 7, None),
    ("qwen3.5", None, None), ("llama3.2", None, None),
])
def test_parse_params(name, total, active):
    s = parse_params(name)
    assert (s.total_b is None) == (total is None)
    if total is not None:
        assert s.total_b == pytest.approx(total)
    assert s.active_b == (pytest.approx(active) if active else None)


def test_parse_size_string():
    assert parse_size_string("8.0B") == 8.0
    assert parse_size_string("270M") == pytest.approx(0.27)
    assert parse_size_string(None) is None


@pytest.mark.parametrize("model,tier", [
    ("qwen3.5:0.8b", "tiny"), ("gemma3:270m", "tiny"), ("qwen3.5:2b", "small"), ("qwen3.5:4b", "small"),
    ("qwen3.5:9b", "medium"), ("qwen3.6:35b-a3b", "medium"), ("gpt-oss:20b", "medium"),
    ("qwen3.6:27b", "large"), ("qwen3.5:122b-a10b", "large"), ("gpt-oss:120b", "large"),
    ("llama3.3:70b", "xl"), ("qwen3.5:397b-a17b", "xl"), ("mystery-model", "medium"),
])
def test_local_tiers(policy, model, tier):
    a = AgentSpec(name="x", kind="local", provider="ollama", model=model, base_url="http://h")
    assert resolve_tier(a, policy).tier == tier


def test_tier_sources_and_overrides(policy):
    assert resolve_tier(policy.agents["claude"], policy).tier == "frontier"
    api = AgentSpec(name="s", kind="api", provider="anthropic", model="claude-haiku-5-5", base_url="u")
    assert resolve_tier(api, policy).tier == "frontier"
    manual = AgentSpec(name="m", kind="local", provider="ollama", model="qwen3.5:0.8b", tier="medium", base_url="u")
    info = resolve_tier(manual, policy)
    assert info.tier == "medium" and info.source == "manual"
    meta = AgentSpec(name="m", kind="local", provider="ollama", model="custom", params_b=26.9, base_url="u",
                     context_window=262144)
    info = resolve_tier(meta, policy)
    assert info.tier == "large" and info.source == "size:metadata"
    assert info.context_tokens == 32768  # min(model window, tier num_ctx)
    # openrouter is not first-party: size from the name
    orr = AgentSpec(name="o", kind="api", provider="openrouter", model="qwen/qwen3.5-4b", base_url="u")
    assert resolve_tier(orr, policy).tier == "small"


SHOW_LLAMA = {"details": {"family": "llama", "parameter_size": "8.0B", "quantization_level": "Q4_0"},
              "model_info": {"general.architecture": "llama", "general.parameter_count": 8030261248,
                             "llama.context_length": 8192}}
SHOW_MOE = {"details": {"family": "qwen3moe", "parameter_size": "30.5B", "quantization_level": "Q4_K_M"},
            "model_info": {"general.architecture": "qwen3moe", "general.parameter_count": 30532122624,
                           "qwen3moe.context_length": 262144, "qwen3moe.expert_count": 128,
                           "qwen3moe.expert_used_count": 8}}


def test_parse_ollama_show():
    m = parse_ollama_show("llama3.1:8b", SHOW_LLAMA)
    assert m["params_b"] == pytest.approx(8.03, abs=0.01) and m["context_window"] == 8192
    assert m["quantization"] == "Q4_0" and m["active_params_b"] is None
    m = parse_ollama_show("qwen3:30b-a3b", SHOW_MOE)
    assert m["params_b"] == pytest.approx(30.5, abs=0.1) and m["active_params_b"] == 3 and m["context_window"] == 262144


def fake_transport(routes):
    calls = []

    def t(method, url, headers, body, timeout):
        calls.append((method, url, body))
        key = (method, url.split("://", 1)[1].split("/", 1)[1])
        if key not in routes:
            raise OSError("connection refused")
        r = routes[key]
        return (200, r(body) if callable(r) else r)
    t.calls = calls
    return t


def test_ollama_discovery_uses_api_show():
    t = fake_transport({
        ("GET", "api/tags"): {"models": [{"name": "llama3.1:8b", "details": {"parameter_size": "8.0B"}},
                                         {"name": "qwen3:30b-a3b", "details": {"parameter_size": "30.5B"}}]},
        ("POST", "api/show"): lambda b: SHOW_LLAMA if b["model"] == "llama3.1:8b" else SHOW_MOE,
    })
    ms = ollama_models("http://localhost:11434", transport=t)
    assert [m.model for m in ms] == ["llama3.1:8b", "qwen3:30b-a3b"]
    assert ms[0].context_window == 8192 and ms[0].source == "api/show"
    assert ms[1].active_params_b == 3 and ms[1].context_window == 262144


def test_discovery_unreachable_is_empty():
    t = fake_transport({})
    assert ollama_models("http://localhost:11434", transport=t) == []
    assert lmstudio_models("http://localhost:1234", transport=t) == []


def test_lmstudio_v1_then_v0():
    v1 = fake_transport({("GET", "api/v1/models"): {"models": [
        {"type": "llm", "key": "qwen/qwen3.5-9b", "params_string": "9B", "max_context_length": 262144,
         "quantization": {"name": "Q4_K_M"}},
        {"type": "embedding", "key": "nomic-embed"}]}})
    ms = lmstudio_models("http://localhost:1234", transport=v1)
    assert len(ms) == 1 and ms[0].params_b == 9 and ms[0].quantization == "Q4_K_M"
    assert ms[0].base_url == "http://localhost:1234/v1"
    v0 = fake_transport({("GET", "api/v0/models"): {"data": [
        {"id": "meta-llama-3.1-8b-instruct", "type": "llm", "max_context_length": 131072, "quantization": "Q4_K_M"}]}})
    ms = lmstudio_models("http://localhost:1234/v1", transport=v0)
    assert ms[0].params_b == 8 and ms[0].context_window == 131072 and ms[0].source == "api/v0/models"


def test_openai_compatible_vllm_and_llamacpp():
    t = fake_transport({("GET", "v1/models"): {"data": [
        {"id": "Qwen/Qwen3.6-27B", "max_model_len": 65536},
        {"id": "model.gguf", "meta": {"n_params": 4021000000, "n_ctx_train": 32768}}]}})
    ms = openai_compatible_models("http://localhost:8000/v1", transport=t)
    assert ms[0].params_b == 27 and ms[0].context_window == 65536
    assert ms[1].params_b == pytest.approx(4.021) and ms[1].context_window == 32768
