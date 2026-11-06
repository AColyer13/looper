# Model tiers, team size, and local models

Research notes behind looper v0.3's capability tiers, team-size limits and local-model handling.
Sources were checked on **2026-11-05**. Model lineups change every few months, so treat the names as a
snapshot and rely on the auto-detection plus per-agent `tier` overrides.

## TL;DR

- **Parameter count is a useful but weak predictor.** Purpose-built 1–3B tool callers beat general
  models twice their size, and rankings within one family aren't monotonic ([1], [2], [3]). looper uses
  size to pick a *default* tier, and you can override it per agent (`"tier": "medium"` or `--agent ollama:x#medium`).
- **MoE models:** memory scales with *total* params and speed with *active* params ([7]). For the tier
  estimate looper uses `sqrt(total × active)`, a common rule of thumb (it puts 35B‑A3B at about a 10B dense model).
- **Team size: sweet spot 3, hard cap 5.** Adding more agents running the same model levels off fast
  (around 4) and can even make results worse. Mixing different models helps more: 2 different agents can
  match 16 copies of one ([4], [5], [6]).
- **Local models run one at a time per GPU.** Ollama handles 1 request per model by default
  (`OLLAMA_NUM_PARALLEL=1`), each loaded model must fit in VRAM, and running requests in parallel multiplies
  memory use ([9]).

## Tier table

"Effective size" = total params for dense models, `sqrt(total × active)` for MoE. Memory figures are
weights-only GGUF sizes (UD‑Q4_K_XL / Q8_0) from [7] unless noted; add KV cache on top.

| Tier | Effective size | Real models (2026) | Instructions / JSON | Tool calling | Plan / judge / orchestrate | Coding | Weights Q4 / Q8 | looper limits |
|---|---|---|---|---|---|---|---|---|
| **tiny** | < 1.5B | Qwen3.5‑0.8B, Qwen3‑0.6B, Gemma 3 270M, Llama 3.2 1B | Simple single instructions. Raw JSON almost never parses (5 of 1,000 in a sub‑2B study); needs a repair parser [3] | Qwen3‑0.6B: 45.8% BFCL overall, **1.4% multi-turn** [1] | No | No | 0.8B: 0.56 / 0.81 GB | roles: summarize/classify/format only; ctx 4k; out 1k; iterations ×0.34; no fan‑out |
| **small** | 1.5–5B | Qwen3.5‑2B / 4B, Gemma 4 E2B/E4B, Phi‑4‑mini 3.8B, Qwen3‑4B | Good on focused prompts, fair JSON | Qwen3‑4B: 62% BFCL, 35% multi-turn [1]; most sub‑4B fail judgment/negation prompts [2] | No | Small single-file edits | 4B: 2.9 / 4.5 GB | + coder/tester/editor/worker; no planner/judge; ctx 8k; out 4k; ×0.67; ≤3 subtasks |
| **medium** | 5–15B | Qwen3.5‑9B, Qwen3‑14B, gpt‑oss‑20b (21B/3.6B active), Qwen3.6‑35B‑A3B, Gemma 4 26B‑A4B | Reliable; good JSON | Qwen3.5‑9B ≈ 61–64% BFCL [11] | Plans small tasks; judges **with care** (7B judges agree less with humans than 70B [12]) | Qwen3.5‑9B claims 60 on SWE‑bench Verified but failed hands-on agentic tests [13] | 9B: 6.0 / 9.5 GB; gpt‑oss‑20b fits in 16 GB [8]; 35B‑A3B: 22 / 37 GB | + planner/reviewer/judge; not orchestrator; ctx 16k; out 4k |
| **large** | 15–40B | Qwen3.6‑27B, Qwen3.8‑27B, Gemma 4 31B, gpt‑oss‑120b (117B/5.1B), Qwen3.5‑122B‑A10B, Qwen3.8‑Flash‑Next (125B/6B) | Reliable | Reliable | Yes, all roles | Qwen3.6‑27B: 77.2 SWE‑bench Verified (vendor) [13] | 27B: 17.6 / 28.6 GB; gpt‑oss‑120b fits one 80 GB GPU [8]; 122B‑A10B: 68 / 130 GB | all roles incl. orchestrator; ctx 32k; out 8k |
| **xl** | 40B+ | Llama 3.3 70B, Llama 4 Scout (109B/17B), Qwen3.5‑397B‑A17B, Qwen3.8‑2.4T‑A95B | Reliable | Reliable | Yes; 70B judges match humans closely [12] | Strong | 397B: 219 / 421 GB | all roles; ctx 64k; out 8k |
| **frontier** | hosted | Claude Opus/Sonnet/Haiku 5.5, Fable 5.1; GPT‑6 Astra / 6.1 Sol / 6 Luna; Grok 4.7; Gemini 4 Argon / 3.8 Flash; all agent CLIs | — | — | — | — | — | all roles; uses the model's window (0.5–1M tokens) |

Context windows: current open models advertise long native windows (Qwen3.5/3.6/3.8: 262K; gpt‑oss: 128K;
[7], [8], [10]). What you *actually* get locally is whatever the server allocates. Ollama now picks a
default based on VRAM: **4k tokens under 24 GiB**, 32k at 24–48 GiB, 256k at 48 GiB or more [9]. looper
therefore sends an explicit `num_ctx` per tier (Ollama) and trims each `looper run`
prompt to a char budget driven by the **model's declared window** when present:

```
if context_window / num_ctx / context_tokens is set:
    whole = int(tokens × chars_per_token × context_fill)   # defaults: 3.5 × 0.75
else:
    whole = tier.max_context_chars                         # tier ceiling as default
```

A 256k-token model therefore gets about `262144 × 3.5 × 0.75 ≈ 688k` chars of
input budget even when its capability tier is `small`/`medium` (whose tier
ceilings are much smaller). The 0.75 fill leaves ~25% of the window for the
model's output. `looper ask` still does not char-clip message history by tier; enable `history_trim` (see README) for local models with **≤32k** context so long tool traces fit the window.

## The user's example names vs real models

| You said | Real? | Closest real models (verified) | looper tier |
|---|---|---|---|
| "0.2B" | No 0.2B Qwen exists | Gemma 3 270M / FunctionGemma 270M (0.27B) [2]; smallest Qwen is **Qwen3.5‑0.8B** [7] | tiny |
| "4B" | Yes | **Qwen3.5‑4B** (4.21B, Mar 2026) [7], [10] | small |
| "Qwen 3.5 9B" | **Yes** | **Qwen3.5‑9B** (8.96B dense, 262K ctx, Apache‑2.0) [7], [10] | medium |
| "Qwen 3.8 125B" | Not under that exact name | **Qwen3.8‑Flash‑Next**: 125B total / **6B active**, plus 51B n‑gram embedding (~180B on disk), released 2026‑08‑26 under a custom license [14]. The other open Qwen3.8 models are **27B** (Apache‑2.0) and **2.4T‑A95B** [14], [15]. Nearest "~120B" Qwen3.5: **122B‑A10B** [7] | large (≈27B effective). Override with `#xl` if your tests show it's stronger |

Qwen3.7 was API-only, with no open weights [14], [15].

## How looper detects size and context

| Server | Endpoint | Fields used |
|---|---|---|
| Ollama | `GET /api/tags` | `models[].name`, `details.parameter_size`, `details.quantization_level` [16] |
| Ollama | `POST /api/show {"model": …}` | `model_info["general.parameter_count"]`, `model_info["<arch>.context_length"]`, `<arch>.expert_count` (MoE); arch from `general.architecture` [16] |
| LM Studio | `GET /api/v1/models` | `key`, `params_string`, `max_context_length`, `quantization.name`, `loaded_instances[].config.context_length` [17] |
| LM Studio (older) | `GET /api/v0/models` | `id`, `arch`, `quantization`, `max_context_length` [18] |
| vLLM | `GET /v1/models` | `max_model_len` [19] |
| llama.cpp server | `GET /v1/models` | `meta.n_params`, `meta.n_ctx_train` (runtime `n_ctx` at `/props`) [19] |
| any | model name | `qwen3:8b`, `0.6B`, `270m`, `30b-a3b` / `35B-A3B` (MoE total+active), `8x7b`, `e4b`, plus known MoE without a suffix (gpt‑oss, Llama 4) |

Ollama doesn't report active params for MoE. looper takes them from the name (`…-a3b`) or a small built-in table.

## How many agents?

**Recommendation: sweet spot 3, hard cap 5** (`defaults.recommended_agents`, `defaults.max_agents`;
the code refuses more than 8 even if you raise the cap).

- Adding more agents running the same model levels off quickly: in vote and debate setups the gain per
  extra agent drops toward zero, sometimes going negative. **2 different agents matched 16 identical ones** [4].
- A sequential multi-agent study found a best team size beyond which coordination overhead wins, with
  token cost growing roughly with the square of team size [5]. Majority-vote math gains are largest from n=1 to 5 [6].
- Every extra hosted agent adds its own rate limits, keys and bill. Every extra local model must fit in
  VRAM or it gets swapped out [9].
- looper's loops have 5 role groups (plan/orchestrate, research, code, test, review/judge). Past 5,
  extra agents only serve as fallbacks.
- **What 3 looks like:** a strong planner/judge, a coder (a CLI that can edit files), and a reviewer
  from a *different vendor*. looper prefers a different vendor for review and for Best-of-N candidates.

How the loops adapt from 1 to 5 agents (see `docs/team_spec.json` for the exact algorithm):

| Team | What changes |
|---|---|
| 1 agent | Every role goes to it. Best-of-N: a hosted or local medium+ model samples 2× at temperature 0.2 and 0.9; a CLI or small model collapses to 1 candidate and the judge reviews its own work. Orchestrator→workers runs subtasks one after another. |
| 2 | The reviewer/judge is the other agent (diversity). Best-of-N uses 2 candidates. |
| 3 | The full loop from the "what 3 looks like" example. Best-of-N uses 3 candidates (the playbook default). |
| 4–5 | Extra agents become fallbacks and parallel workers. looper warns that returns diminish. |
| Small/tiny only | "Lite mode": optional stages skipped, `plan` merged into `explore`, fewer iterations, simpler prompts, under-tier stages flagged. |

## Local models: what works best

1. **Run sequentially on one GPU.** looper defaults to `max_parallel_local: 1` per local server.
   Ollama's own defaults are `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MAX_LOADED_MODELS` = 3 per GPU, with each model
   needing to fit fully in VRAM [9].
2. **Prefer one capable model over several weak ones.** A single large model (27B dense, or 35B‑A3B MoE)
   on 24–32 GB covers every role. A 9B on 8–12 GB is a decent coder but a shaky planner/judge [11], [13].
3. **Mix in one hosted model if you can.** Use local for the many cheap calls (coding, workers, drafts)
   and a hosted frontier model for the few lead calls (plan, judge). Hosted Qwen is also cheap if
   privacy isn't a concern [15].
4. **Set context deliberately.** Ollama's VRAM-based default can be as small as 4k [9]. looper requests
   a tier-sized `num_ctx` by default; set the agent's `context_window` / `num_ctx` (e.g. 262144) to
   raise the `looper run` clip budget to that window × 3.5 × 0.75.
5. **Leave room for thinking.** Thinking models spend output tokens on reasoning, and caps under about
   2–4k can come back empty [15]. looper's small+ tiers use ≥ 4k output tokens, turns thinking off for
   tiny/small on Ollama (`think: false`), and reports "empty response" so the fallback agent takes over.
6. **Local (and API) agents are text-only.** They can't read your repo or run tests. looper puts
   tool stages (explore, code, run tests, commit) on CLI agents when the team has one. Otherwise it asks
   the model for diffs and commands and says so in the plan.

## Sources

1. TinyLLM: Evaluation and Optimization of Small Language Models for Agentic Tasks on Edge Devices. https://arxiv.org/html/2511.22138
2. Local Agent Bench: Testing 21 Open-Weight Models on Tool Calling (Feb 2026). https://mikeveerman.be/blog/github-2026-02-06-tool-calling-benchmark/
3. Beyond Fluent Generation: CPU Reliability Benchmark for MCP-Style Tool Calling in Sub-2B SLMs. https://arxiv.org/pdf/2609.07370.pdf
4. Understanding Agent Scaling in LLM-Based Multi-Agent Systems via Diversity. https://arxiv.org/html/2602.03794
5. Scaling Behavior of Single LLM-Driven Multi-Agent Systems. https://arxiv.org/html/2606.00655
6. More Agents Improve Math Problem Solving but Adversarial Robustness Gap Persists (ACL Findings 2026). https://aclanthology.org/2026.findings-acl.2158/
7. LlamaBarn PR #63 (Qwen3.5 sizes, params, GGUF Q4/Q8 sizes). https://github.com/ggml-org/LlamaBarn/pull/63 and Qwen3.5 collection https://huggingface.co/collections/Qwen/qwen35
8. Introducing gpt-oss (OpenAI). https://openai.com/index/introducing-gpt-oss/
9. Ollama FAQ (concurrency, VRAM) https://docs.ollama.com/faq and Context length https://docs.ollama.com/context-length
10. Artificial Analysis: Qwen3.5 small models. https://artificialanalysis.ai/articles/qwen3-5-small-models
11. small-llm-tool-use-bench (BFCL on Qwen3.5-9B, Gemma 4, etc.). https://huggingface.co/Manojb/small-llm-tool-use-bench
12. Judging the Judges: Evaluating Alignment and Vulnerabilities in LLMs-as-Judges. https://arxiv.org/html/2406.12624
13. Qwen3.6-27B model card https://huggingface.co/Qwen/Qwen3.6-27B and hands-on Qwen3.5 coding-agent test https://nowokay.hatenablog.com/entry/2026/03/08/205807
14. Qwen models explained: Qwen3.8 Max, Flash and 27B (Sep 2026). https://www.thinkfacility.com/blog/qwen-models-explained-qwen3-8-max-flash-27b/
15. Which Qwen should I run? (graphometer, measured local runs). https://graphometer.ai/qwen/
16. Ollama API: show model details. https://docs.ollama.com/api-reference/show-model-details and https://github.com/ollama/ollama/blob/main/docs/api.md
17. LM Studio REST: list models. https://lmstudio.ai/docs/developer/rest/list
18. LM Studio REST API v0. https://lmstudio.ai/docs/developer/rest/endpoints
19. vLLM /v1/models metadata https://github.com/vllm-project/vllm/pull/45950 and llama.cpp metadata discussion https://github.com/ggml-org/llama.cpp/discussions/9341

Hosted model IDs in the setup menu were checked against the vendor pages: Anthropic
https://platform.claude.com/docs/en/models/overview · OpenAI https://developers.openai.com/api/docs/models ·
xAI https://docs.x.ai/developers/models · Google https://ai.google.dev/gemini-api/docs/models ·
OpenAI-compatible base URLs: https://ai.google.dev/gemini-api/docs/openai
