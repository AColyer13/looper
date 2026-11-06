"""Regenerate src/looper/data/default_config.json.

That JSON file is the source of truth at runtime; this script keeps the
big playbook readable as Python. For a local copy, run ``looper init``.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CTX = (
    "TASK:\n{{task}}\n\n"
)

def stage(name, role, prompt, **kw):
    d = {"name": name, "role": role, "prompt": prompt}
    d.update(kw)
    return d

def r(pattern, weight=1):
    """Regex route rule."""
    return {"regex": pattern, "weight": weight}

def k(word, weight=1):
    """Keyword (word-boundary, case-insensitive) route rule."""
    return {"keyword": word, "weight": weight}

LOOPS = {}

LOOPS["react"] = {
    "description": "ReAct: interleave Thought -> Action -> Observation until a final answer. General-purpose default.",
    "priority": 0,
    "route": [k("quick", 1), k("explain", 1), r(r"\bhow (do|can|should) i\b", 1), r(r"\bwhat (is|are)\b", 1)],
    "stages": [
        stage("step", "generalist",
              CTX + "Work in ReAct style. Iteration {{iteration}}.\n"
              "Previous steps:\n{{outputs.step}}\n\n"
              "Write one Thought, take the Action(s) you need (read files, run commands), "
              "and record the Observation. When the task is fully done, end with a line "
              "starting with 'FINAL ANSWER:' followed by the answer.",
              artifact=True),
    ],
    "iterate": {"body": ["step"], "max_iterations": 6, "exit_signals": ["FINAL ANSWER:"]},
    "caps": {"max_total_calls": 8, "max_wall_seconds": 1800},
}

LOOPS["explore_plan_code_commit"] = {
    "description": "Explore -> Plan -> Code -> Commit. Read the codebase first, plan, implement with a verify loop, then commit.",
    "priority": 5,
    "route": [k("implement", 3), k("build", 2), k("feature", 2), k("add", 1), k("create", 1),
              k("endpoint", 1), k("refactor", 1), r(r"\b(rate[- ]?limiter|cache|middleware|cli|api)\b", 1)],
    "stages": [
        stage("explore", "explorer",
              CTX + "EXPLORE only: read the relevant files and summarize the code structure, conventions, "
              "and every place this task touches. Do NOT write code yet."),
        stage("plan", "planner",
              CTX + "Exploration notes:\n{{outputs.explore}}\n\n"
              "Write a concrete step-by-step implementation plan (files, functions, tests). No code yet."),
        stage("code", "coder",
              CTX + "Plan:\n{{outputs.plan}}\n\nReviewer feedback from the last round (if any):\n{{outputs.verify}}\n\n"
              "Implement the plan in the working tree. Run the tests/linters you can.",
              artifact=True),
        stage("verify", "reviewer",
              CTX + "Plan:\n{{outputs.plan}}\n\nImplementation report:\n{{outputs.code}}\n\n"
              "Review the actual changes in the working tree against the plan. Run tests. "
              "If everything is correct reply with 'LGTM' on its own line; otherwise list concrete fixes."),
        stage("commit", "committer",
              CTX + "Final review:\n{{outputs.verify}}\n\n"
              "Stage the relevant changes and create a git commit with a clear conventional-commit message. "
              "Do not push. Report the commit hash."),
    ],
    "iterate": {"body": ["code", "verify"], "max_iterations": 3, "exit_signals": ["LGTM"], "check_stages": ["verify"]},
    "caps": {"max_total_calls": 12, "max_wall_seconds": 3600},
}

LOOPS["research_plan_implement"] = {
    "description": "Research -> Plan -> Implement. For unfamiliar libraries/APIs/migrations: gather facts before writing code.",
    "priority": 4,
    "route": [k("integrate", 3), k("migrate", 3), k("migration", 3), k("sdk", 2), k("library", 1),
              k("unfamiliar", 3), k("upgrade", 2), r(r"\bresearch\b.*\b(then|and)\b.*\b(implement|build|integrate)\b", 5)],
    "stages": [
        stage("research", "researcher",
              CTX + "RESEARCH: find current docs, APIs, versions, gotchas and examples relevant to this task. "
              "Cite sources. Do not write project code."),
        stage("plan", "planner",
              CTX + "Research:\n{{outputs.research}}\n\nWrite an implementation plan grounded in the research."),
        stage("implement", "coder",
              CTX + "Plan:\n{{outputs.plan}}\n\nResearch:\n{{outputs.research}}\n\nReview feedback (if any):\n{{outputs.review}}\n\n"
              "Implement the plan in the working tree and run tests.", artifact=True),
        stage("review", "reviewer",
              CTX + "Plan:\n{{outputs.plan}}\n\nImplementation report:\n{{outputs.implement}}\n\n"
              "Review the changes. Reply 'LGTM' if correct, else list fixes."),
    ],
    "iterate": {"body": ["implement", "review"], "max_iterations": 3, "exit_signals": ["LGTM"], "check_stages": ["review"]},
    "caps": {"max_total_calls": 10, "max_wall_seconds": 3600},
}

LOOPS["spec_driven"] = {
    "description": "Spec-driven development: write a spec with acceptance criteria, review it, then implement until the spec is satisfied.",
    "priority": 6,
    "route": [k("spec", 4), k("specification", 4), k("prd", 4), k("rfc", 3), r(r"\bacceptance criteria\b", 4),
              k("requirements", 2), k("design doc", 3)],
    "stages": [
        stage("spec", "planner",
              CTX + "Write a precise SPEC: goals, non-goals, interfaces, data shapes, edge cases, and numbered "
              "ACCEPTANCE CRITERIA. Save it as SPEC.md if working in a repo."),
        stage("spec_review", "reviewer",
              CTX + "Spec:\n{{outputs.spec}}\n\nCritique the spec for ambiguity and missing cases; output the corrected spec in full."),
        stage("implement", "coder",
              CTX + "Spec:\n{{outputs.spec_review}}\n\nConformance feedback (if any):\n{{outputs.conformance}}\n\n"
              "Implement exactly to the spec. Run tests.", artifact=True),
        stage("conformance", "reviewer",
              CTX + "Spec:\n{{outputs.spec_review}}\n\nImplementation report:\n{{outputs.implement}}\n\n"
              "Check every acceptance criterion against the working tree. If all pass reply 'SPEC SATISFIED'; "
              "otherwise list each failing criterion."),
    ],
    "iterate": {"body": ["implement", "conformance"], "max_iterations": 3, "exit_signals": ["SPEC SATISFIED"], "check_stages": ["conformance"]},
    "caps": {"max_total_calls": 10, "max_wall_seconds": 3600},
}

LOOPS["evaluator_optimizer"] = {
    "description": "Evaluator-Optimizer: a generator produces, an evaluator grades against criteria, the optimizer revises until it passes.",
    "priority": 3,
    "route": [k("improve", 2), k("optimize", 2), k("optimise", 2), k("refine", 2), k("polish", 2), k("iterate", 1),
              k("quality", 1), r(r"\bmake (it|this) better\b", 2)],
    "stages": [
        stage("generate", "coder", CTX + "Produce the best first version of the deliverable.", artifact=True),
        stage("evaluate", "evaluator",
              CTX + "Current version:\n{{previous_artifact}}\n\n"
              "Grade it 1-10 against correctness, clarity, and fitness for the task. "
              "If it is 9+ and has no blocking issues reply 'LGTM'; otherwise list the specific improvements."),
        stage("optimize", "coder",
              CTX + "Current version:\n{{previous_artifact}}\n\nEvaluator feedback:\n{{outputs.evaluate}}\n\n"
              "Produce an improved full version addressing every point.", artifact=True),
    ],
    "iterate": {"body": ["evaluate", "optimize"], "max_iterations": 4, "exit_signals": ["LGTM"], "check_stages": ["evaluate"]},
    "caps": {"max_total_calls": 10, "max_wall_seconds": 2700},
}

LOOPS["best_of_n"] = {
    "description": "Best-of-N: N candidates generated in parallel (rotating across installed CLIs), then a judge picks or merges the best.",
    "priority": 4,
    "route": [r(r"\bbest[- ]of[- ]?n\b", 6), r(r"\bbest of \d+\b", 6), k("alternatives", 3), k("variants", 3),
              k("brainstorm", 3), r(r"\b(several|multiple|different) (approaches|options|versions|names|designs)\b", 4),
              k("candidates", 2)],
    "stages": [
        stage("candidates", "candidate",
              CTX + "You are candidate #{{candidate}}. Produce a complete, independent solution. "
              "Do not modify shared files; return your solution in the response.",
              type="parallel", n=3),
        stage("judge", "judge",
              CTX + "Candidates:\n{{outputs.candidates}}\n\n"
              "Compare the candidates against the task. State which is best and why, then output the final "
              "chosen (or merged) solution in full under 'WINNER:'.", artifact=True),
    ],
    "caps": {"max_total_calls": 6, "max_wall_seconds": 1800},
}

LOOPS["orchestrator_workers"] = {
    "description": "Orchestrator -> Workers: an orchestrator splits the task into independent subtasks, workers run them in parallel, then results are synthesized.",
    "priority": 3,
    "route": [r(r"\b(across|throughout) (the )?(codebase|repo|repository|project|services)\b", 4), k("monorepo", 3),
              r(r"\b(every|all|each) (file|module|service|package|endpoint)s?\b", 3), r(r"\bin parallel\b", 3),
              k("fan out", 3), k("large", 1), k("subtasks", 3)],
    "stages": [
        stage("orchestrate", "orchestrator",
              CTX + "Split this task into 2-6 INDEPENDENT subtasks that can be done in parallel without conflicts. "
              "Output ONLY a JSON array of strings, one subtask per element."),
        stage("workers", "worker",
              CTX + "You are a worker. Your subtask:\n{{item}}\n\nDo only this subtask and report exactly what you changed.",
              type="map", source="orchestrate", max_items=6, max_workers=4),
        stage("synthesize", "planner",
              CTX + "Worker reports:\n{{outputs.workers}}\n\nVerify the pieces fit together, fix any integration gaps, "
              "and write a final summary.", artifact=True),
    ],
    "caps": {"max_total_calls": 10, "max_wall_seconds": 3600},
}

LOOPS["pdca"] = {
    "description": "PDCA (Plan-Do-Check-Act): plan a change with a measurable target, do it, check against the target, act/standardize; repeat.",
    "priority": 2,
    "route": [k("pdca", 6), k("process", 2), k("workflow", 1), r(r"\bcontinuous(ly)? improv", 3), k("kpi", 3),
              k("metrics", 2), r(r"\breduce\b.*\b(time|latency|cost|errors|churn)\b", 3), k("experiment", 2)],
    "stages": [
        stage("plan", "planner",
              CTX + "Results of the last cycle (if any):\n{{outputs.act}}\n\nPLAN: state the hypothesis, the change, "
              "and a measurable TARGET for this cycle (cycle {{iteration}})."),
        stage("do", "coder", CTX + "Plan:\n{{outputs.plan}}\n\nDO: carry out the planned change.", artifact=True),
        stage("check", "evaluator",
              CTX + "Plan:\n{{outputs.plan}}\n\nWhat was done:\n{{outputs.do}}\n\nCHECK: measure the result against "
              "the TARGET. If met reply 'TARGET MET', else explain the gap."),
        stage("act", "planner",
              CTX + "Check results:\n{{outputs.check}}\n\nACT: standardize what worked and decide the next adjustment."),
    ],
    "iterate": {"body": ["plan", "do", "check", "act"], "max_iterations": 3, "exit_signals": ["TARGET MET"], "check_stages": ["check"]},
    "caps": {"max_total_calls": 12, "max_wall_seconds": 3600},
}

LOOPS["tdd"] = {
    "description": "Test-Driven Development: write a failing test (red), implement until green, then refactor.",
    "priority": 7,
    "route": [k("tdd", 8), k("test", 4), k("tests", 4), r(r"\bunit[- ]tests?\b", 2), k("coverage", 3),
              r(r"\bred[ -/]green\b", 5), r(r"\badd (a |some )?(failing )?tests?\b", 3), k("pytest", 2), k("jest", 2)],
    "stages": [
        stage("write_test", "tester",
              CTX + "RED: write the failing test(s) that capture the required behavior. Run them and confirm they "
              "fail for the right reason. Do not implement the feature."),
        stage("implement", "coder",
              CTX + "Tests written:\n{{outputs.write_test}}\n\nLast test run (if any):\n{{outputs.run_tests}}\n\n"
              "GREEN: write the minimal code to make the tests pass. Do not weaken the tests.", artifact=True),
        stage("run_tests", "tester",
              CTX + "Run the full relevant test suite and report results. If every test passes, include the exact "
              "line 'ALL TESTS PASS'; otherwise paste the failures."),
        stage("refactor", "reviewer",
              CTX + "REFACTOR: clean up the implementation and tests without changing behavior, re-run tests, "
              "and summarize the final state."),
    ],
    "iterate": {"body": ["implement", "run_tests"], "max_iterations": 5, "exit_signals": ["ALL TESTS PASS"], "check_stages": ["run_tests"]},
    "caps": {"max_total_calls": 14, "max_wall_seconds": 3600},
}

LOOPS["incident_response"] = {
    "description": "Incident response: triage -> diagnose -> mitigate/verify loop -> postmortem. Mitigate first, root-cause second.",
    "priority": 10,
    "route": [k("incident", 5), k("outage", 5), k("production", 3), k("prod", 3), r(r"\b(is|are|went) down\b", 4),
              k("down", 2), r(r"\b5\d\d\b", 2), r(r"\b5xx\b", 3), k("sev1", 5), k("sev2", 4), k("pagerduty", 4),
              k("paged", 3), r(r"\blatency spike\b", 3), k("rollback", 2), k("on-call", 3)],
    "stages": [
        stage("triage", "responder",
              CTX + "TRIAGE: assess impact and severity, what changed recently (deploys, config, deps), and the "
              "fastest safe mitigation options. Be brief."),
        stage("diagnose", "investigator",
              CTX + "Triage:\n{{outputs.triage}}\n\nDIAGNOSE: inspect logs/metrics/code to find the likely cause. "
              "List evidence for and against each suspect."),
        stage("mitigate", "coder",
              CTX + "Diagnosis:\n{{outputs.diagnose}}\n\nVerification so far:\n{{outputs.verify}}\n\n"
              "MITIGATE: apply the smallest safe fix or rollback. Prefer reversible actions. Report exactly what changed.",
              artifact=True),
        stage("verify", "responder",
              CTX + "Mitigation:\n{{outputs.mitigate}}\n\nVERIFY: confirm the symptom is gone (tests, health checks, "
              "logs). If resolved reply 'MITIGATED'; otherwise say what still fails."),
        stage("postmortem", "writer",
              CTX + "Triage:\n{{outputs.triage}}\nDiagnosis:\n{{outputs.diagnose}}\nMitigation:\n{{outputs.mitigate}}\n"
              "Verification:\n{{outputs.verify}}\n\nWrite a blameless postmortem: timeline, root cause, impact, "
              "what went well, action items."),
    ],
    "iterate": {"body": ["mitigate", "verify"], "max_iterations": 3, "exit_signals": ["MITIGATED", "RESOLVED"], "check_stages": ["verify"]},
    "caps": {"max_total_calls": 10, "max_wall_seconds": 1800},
}

LOOPS["musk_algorithm"] = {
    "description": "Musk's 5-step algorithm: question every requirement, delete parts/process, simplify/optimize, accelerate cycle time, then automate.",
    "priority": 5,
    "route": [k("musk", 8), k("simplify", 4), r(r"\b(delete|remove|cut)\b.*\b(code|steps|process|features|dependencies)\b", 3),
              k("streamline", 3), k("bloat", 3), k("bloated", 3), r(r"\bover[- ]?engineered\b", 4),
              r(r"\bquestion (the|every|all) requirements?\b", 5), k("automate", 2)],
    "stages": [
        stage("question_requirements", "skeptic",
              CTX + "STEP 1 - Make the requirements less dumb: list every requirement/assumption, who owns it, "
              "and which ones are wrong, unnecessary, or unverified."),
        stage("delete", "planner",
              CTX + "Requirement review:\n{{outputs.question_requirements}}\n\nSTEP 2 - Delete: list every part, "
              "step, file, or dependency to remove. If you are not adding back at least 10%, you are not deleting enough."),
        stage("simplify", "coder",
              CTX + "Deletions:\n{{outputs.delete}}\n\nSTEP 3 - Simplify/optimize what remains (only after deleting). "
              "Apply the changes.", artifact=True),
        stage("accelerate", "coder",
              CTX + "Current state:\n{{outputs.simplify}}\n\nSTEP 4 - Accelerate cycle time: speed up the build/test/"
              "feedback loop and the hot paths. Apply the changes.", artifact=True),
        stage("automate", "coder",
              CTX + "Current state:\n{{outputs.accelerate}}\n\nSTEP 5 - Automate what is now proven and simple "
              "(scripts, CI, hooks). Summarize all five steps at the end.", artifact=True),
    ],
    "caps": {"max_total_calls": 7, "max_wall_seconds": 3600},
}

LOOPS["deep_research"] = {
    "description": "Deep research: scope questions, gather from many sources, check for gaps until coverage is sufficient, then write a cited report.",
    "priority": 6,
    "route": [k("research", 5), k("investigate", 2), k("survey", 3), k("landscape", 3), k("sources", 2),
              k("citations", 3), r(r"\bstate of the art\b", 3), r(r"\bdeep[- ]dive\b", 4), k("compare", 1),
              k("report", 1), k("market", 2)],
    "stages": [
        stage("scope", "planner",
              CTX + "Break the research question into 3-7 sub-questions and the kinds of sources that would answer each."),
        stage("gather", "researcher",
              CTX + "Research plan:\n{{outputs.scope}}\n\nGap analysis from the last round (if any):\n{{outputs.gap_check}}\n\n"
              "Findings so far:\n{{outputs.gather}}\n\nSearch broadly and gather findings with source URLs. "
              "Focus on unanswered sub-questions.", artifact=True),
        stage("gap_check", "evaluator",
              CTX + "Research plan:\n{{outputs.scope}}\n\nFindings:\n{{outputs.gather}}\n\nAre all sub-questions "
              "answered with credible, current sources? If yes reply 'SUFFICIENT'; otherwise list the gaps."),
        stage("report", "writer",
              CTX + "Findings:\n{{outputs.gather}}\n\nWrite the final research report: executive summary, findings per "
              "sub-question, comparison table where useful, recommendations, and cited sources.", artifact=True),
    ],
    "iterate": {"body": ["gather", "gap_check"], "max_iterations": 3, "exit_signals": ["SUFFICIENT"], "check_stages": ["gap_check"]},
    "caps": {"max_total_calls": 9, "max_wall_seconds": 3600},
}

LOOPS["hypothesis_tool_scout"] = {
    "description": "Hypothesis / Tool Scout: generate competing hypotheses (root causes, or candidate tools/approaches), scout each in parallel, pick the winner, apply and verify. Good for bugs and tool selection.",
    "priority": 5,
    "route": [k("bug", 4), k("fix", 3), k("debug", 4), k("broken", 3), k("flaky", 4), r(r"\broot[- ]cause\b", 4),
              k("traceback", 3), k("exception", 2), k("crash", 3), k("regression", 3), k("hypothesis", 5),
              r(r"\bwhich (tool|library|framework|package)\b", 4), r(r"\btool scout\b", 6), r(r"\bwhy (is|does|do)\b", 2)],
    "stages": [
        stage("hypothesize", "planner",
              CTX + "List 2-5 competing hypotheses (likely root causes, or candidate tools/approaches). "
              "Output ONLY a JSON array of strings, each one hypothesis with a cheap way to test it."),
        stage("scout", "scout",
              CTX + "Hypothesis to scout:\n{{item}}\n\nGather evidence for and against it (read code, run a quick "
              "experiment, check docs). Do not make permanent changes. End with 'VERDICT: likely' or 'VERDICT: unlikely'.",
              type="map", source="hypothesize", max_items=5, max_workers=4),
        stage("decide", "judge",
              CTX + "Scout reports:\n{{outputs.scout}}\n\nPick the best-supported hypothesis and describe the fix or choice."),
        stage("apply", "coder",
              CTX + "Decision:\n{{outputs.decide}}\n\nVerification feedback (if any):\n{{outputs.verify}}\n\n"
              "Apply the fix/choice in the working tree. Add a regression test where it makes sense.", artifact=True),
        stage("verify", "tester",
              CTX + "Applied change:\n{{outputs.apply}}\n\nVerify it (tests, repro). If it works reply 'VERIFIED'; "
              "otherwise describe what still fails."),
    ],
    "iterate": {"body": ["apply", "verify"], "max_iterations": 3, "exit_signals": ["VERIFIED"], "check_stages": ["verify"]},
    "caps": {"max_total_calls": 14, "max_wall_seconds": 3600},
}

LOOPS["editor_slop_grader"] = {
    "description": "Editor / AI-slop grader: grade text for AI slop (filler, hedging, clichés, em-dash spam, generic phrasing), then edit and re-grade until clean.",
    "priority": 6,
    "route": [k("slop", 8), r(r"\bai[- ]sounding\b", 5), k("proofread", 4), k("edit", 2), k("rewrite", 3),
              k("prose", 3), k("copy", 1), k("essay", 3), k("blog", 3), k("draft", 2), k("tone", 2),
              k("readme", 1), r(r"\bwriting\b", 1)],
    "stages": [
        stage("grade", "grader",
              CTX + "Grade the text for AI slop on a 0-10 scale (0 = no slop). Flag filler, hedging, clichés, "
              "'delve'-style vocabulary, empty superlatives, list bloat, and em-dash overuse, quoting each instance. "
              "End with 'SLOP SCORE: <n>'."),
        stage("edit", "writer",
              CTX + "Slop report:\n{{outputs.regrade}}\n{{outputs.grade}}\n\nRewrite the text to fix every flagged "
              "issue while keeping meaning and voice. Output the full edited text.", artifact=True),
        stage("regrade", "grader",
              CTX + "Edited text:\n{{outputs.edit}}\n\nRe-grade for AI slop exactly as before. End with 'SLOP SCORE: <n>'."),
    ],
    "iterate": {"body": ["edit", "regrade"], "max_iterations": 3, "exit_signals": [r"re:SLOP SCORE:\s*[0-2]\b"], "check_stages": ["regrade"]},
    "caps": {"max_total_calls": 8, "max_wall_seconds": 1800},
}

# --------------------------------------------------------------------------- v0.3: teams, tiers, roles
# Stages that may be dropped (optional) or folded into the previous stage (mergeable)
# when the team has no capable agent for them, or the team is all tiny/small models.
OPTIONAL = {
    ("explore_plan_code_commit", "commit"), ("spec_driven", "spec_review"), ("tdd", "refactor"),
    ("incident_response", "postmortem"), ("musk_algorithm", "accelerate"), ("musk_algorithm", "automate"),
}
MERGEABLE = {
    ("explore_plan_code_commit", "plan"), ("research_plan_implement", "plan"), ("musk_algorithm", "delete"),
    ("hypothesis_tool_scout", "decide"), ("orchestrator_workers", "synthesize"),
}
for lname, spec in LOOPS.items():
    for s in spec["stages"]:
        if (lname, s["name"]) in OPTIONAL:
            s["optional"] = True
        if (lname, s["name"]) in MERGEABLE:
            s["mergeable"] = True

ABSTRACT_ROLES = ["summarizer", "classifier", "formatter", "generalist", "worker", "coder", "tester",
                  "editor", "researcher", "planner", "reviewer", "judge", "orchestrator"]

# concrete stage role -> abstract role. lead=True roles go to the strongest agent;
# tools=True roles need an agent that can read files / run commands (the CLIs).
ROLES = {
    "generalist": {"abstract": "generalist", "tools": True},
    "explorer": {"abstract": "researcher", "tools": True},
    "planner": {"abstract": "planner", "lead": True},
    "coder": {"abstract": "coder", "tools": True},
    "committer": {"abstract": "coder", "tools": True},
    "tester": {"abstract": "tester", "tools": True},
    "reviewer": {"abstract": "reviewer", "lead": True, "diverse_from": "coder"},
    "evaluator": {"abstract": "judge", "lead": True, "diverse_from": "coder"},
    "researcher": {"abstract": "researcher", "tools": True},
    "scout": {"abstract": "researcher", "tools": True},
    "investigator": {"abstract": "researcher", "tools": True},
    "responder": {"abstract": "generalist", "tools": True},
    "orchestrator": {"abstract": "orchestrator", "lead": True},
    "worker": {"abstract": "worker", "tools": True},
    "candidate": {"abstract": "coder"},
    "judge": {"abstract": "judge", "lead": True},
    "skeptic": {"abstract": "reviewer", "lead": True},
    "writer": {"abstract": "editor"},
    "grader": {"abstract": "judge", "lead": True, "diverse_from": "editor"},
}

_T = ["summarizer", "classifier", "formatter"]
_S = _T + ["generalist", "worker", "coder", "tester", "editor", "researcher"]
_M = _S + ["planner", "reviewer", "judge"]
_L = _M + ["orchestrator"]
# Effective size = total params for dense models, sqrt(total * active) for MoE (heuristic).
TIERS = {
    "tiny": {"rank": 0, "max_params_b": 1.5, "roles": _T,
             "examples": ["qwen3.5:0.8b", "gemma3:270m", "qwen3:0.6b", "llama3.2:1b"],
             "num_ctx": 4096, "max_output_tokens": 1024, "max_context_chars": 6000,
             "structured_output": "unreliable", "iteration_multiplier": 0.34, "can_orchestrate": False,
             "max_map_items": 0, "best_of_n": False, "simplify_prompts": True, "disable_thinking": True,
             "note": "Utility only: summarize, classify, reformat. Multi-turn tool calling ~0-2% on BFCL."},
    "small": {"rank": 1, "max_params_b": 5, "roles": _S,
              "examples": ["qwen3.5:2b", "qwen3.5:4b", "gemma4:e4b", "phi4-mini:3.8b", "llama3.2:3b"],
              "num_ctx": 8192, "max_output_tokens": 4096, "max_context_chars": 12000,
              "structured_output": "fair", "iteration_multiplier": 0.67, "can_orchestrate": False,
              "max_map_items": 3, "best_of_n": False, "simplify_prompts": True, "disable_thinking": True,
              "note": "Simple single-file edits and tests; no planning, judging or orchestration."},
    "medium": {"rank": 2, "max_params_b": 15, "roles": _M,
               "examples": ["qwen3.5:9b", "qwen3:14b", "gpt-oss:20b", "qwen3.6:35b-a3b", "gemma4:26b-a4b"],
               "num_ctx": 16384, "max_output_tokens": 4096, "max_context_chars": 24000,
               "structured_output": "good", "iteration_multiplier": 1.0, "can_orchestrate": False,
               "max_map_items": 4, "best_of_n": True, "simplify_prompts": False, "disable_thinking": False,
               "note": "Coder/tester; can plan and judge with care (lower agreement than 70B+ judges)."},
    "large": {"rank": 3, "max_params_b": 40, "roles": _L,
              "examples": ["qwen3.6:27b", "qwen3.8:27b", "gemma4:31b", "gpt-oss:120b", "qwen3.5:122b-a10b"],
              "num_ctx": 32768, "max_output_tokens": 8192, "max_context_chars": 48000,
              "structured_output": "reliable", "iteration_multiplier": 1.0, "can_orchestrate": True,
              "max_map_items": 6, "best_of_n": True, "simplify_prompts": False, "disable_thinking": False,
              "note": "Full roles incl. planner/judge/orchestrator. Best local value per GB."},
    "xl": {"rank": 4, "max_params_b": None, "roles": _L,
           "examples": ["llama3.3:70b", "qwen3.5:397b-a17b", "llama4:scout (109b-a17b)"],
           "num_ctx": 65536, "max_output_tokens": 8192, "max_context_chars": 80000,
           "structured_output": "reliable", "iteration_multiplier": 1.0, "can_orchestrate": True,
           "max_map_items": 6, "best_of_n": True, "simplify_prompts": False, "disable_thinking": False,
           "note": "Near-frontier locally; needs 48GB+ VRAM or large unified memory / expert offload."},
    "frontier": {"rank": 5, "max_params_b": None, "roles": _L,
                 "examples": ["claude-opus-5-5", "gpt-6-astra", "grok-4.7", "gemini-4-argon", "any agent CLI"],
                 "num_ctx": None, "max_output_tokens": 16000, "max_context_chars": 120000,
                 "structured_output": "reliable", "iteration_multiplier": 1.0, "can_orchestrate": True,
                 "max_map_items": 8, "best_of_n": True, "simplify_prompts": False, "disable_thinking": False,
                 "note": "Hosted frontier APIs and agent CLIs. Default tier for kind=cli and first-party APIs."},
}

# Provider presets. Model ids verified against vendor docs on 2026-11-05; all editable.
PROVIDERS = {
    "anthropic": {"kind": "api", "vendor": "claude", "api_style": "anthropic", "base_url": "https://api.anthropic.com/v1",
                  "api_key_env": "ANTHROPIC_API_KEY", "context_window": 1000000,
                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]},
    "openai": {"kind": "api", "vendor": "codex", "api_style": "openai_responses", "base_url": "https://api.openai.com/v1",
               "api_key_env": "OPENAI_API_KEY", "context_window": 1050000,
               "models": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"]},
    "xai": {"kind": "api", "vendor": "grok", "api_style": "openai", "base_url": "https://api.x.ai/v1",
            "api_key_env": "XAI_API_KEY", "context_window": 500000,
            "models": ["grok-4.7", "grok-build-0.1", "grok-4.3"]},
    "google": {"kind": "api", "vendor": "agy", "api_style": "openai",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "api_key_env": "GEMINI_API_KEY", "context_window": 1000000,
               "models": ["gemini-4-argon", "gemini-3.8-flash", "gemini-3.5-flash-lite"]},
    "openrouter": {"kind": "api", "api_style": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "api_key_env": "OPENROUTER_API_KEY", "context_window": None, "models": []},
    "ollama": {"kind": "local", "api_style": "ollama", "base_url": "http://localhost:11434",
               "api_key_env": None, "context_window": None, "models": []},
    "lmstudio": {"kind": "local", "api_style": "openai", "base_url": "http://localhost:1234/v1",
                 "api_key_env": "LM_API_TOKEN", "key_optional": True, "context_window": None, "models": []},
    "openai_compatible": {"kind": "local", "api_style": "openai", "base_url": "http://localhost:8000/v1",
                          "api_key_env": "LOOPER_COMPAT_API_KEY", "key_optional": True,
                          "context_window": None, "models": []},
}

CONFIG = {
    "$schema_version": 3,
    "defaults": {
        "timeout_seconds": 900,
        "max_iterations": 3,
        "caps": {"max_total_calls": 20, "max_wall_seconds": 3600},
        "fallback_agents": ["claude", "codex", "grok", "agy"],
        "fallback_on_error": True,
        "max_context_chars": 20000,
        "runs_dir": ".looper/runs",
        "version_probe_timeout": 3,
        "max_agents": 5,
        "recommended_agents": 3,
        "max_parallel_local": 1,
        "max_best_of_n": 5,
        "single_agent_samples": 2,
        "chars_per_token": 3.5,
        "context_fill": 0.75,
        "unknown_local_tier": "medium",
    },
    "tiers": TIERS,
    "roles": ROLES,
    "providers": PROVIDERS,
    "agents": {
        "claude": {
            "description": "Claude Code (Anthropic)",
            "binary": "claude",
            "command": ["{binary}", "-p", "{prompt}"],
            "version_args": ["--version"],
        },
        "codex": {
            "description": "Codex CLI (OpenAI)",
            "binary": "codex",
            "command": ["{binary}", "exec", "--skip-git-repo-check", "{prompt}"],
            "version_args": ["--version"],
        },
        "grok": {
            "description": "Grok Build CLI (xAI) - headless via -p",
            "binary": "grok",
            "command": ["{binary}", "-p", "{prompt}", "--no-auto-update"],
            "version_args": ["--version"],
        },
        "agy": {
            "description": "Antigravity CLI (Google) - print mode via -p",
            "binary": "agy",
            "command": ["{binary}", "-p", "{prompt}"],
            "version_args": ["--version"],
        },
    },
    "strategies": {
        "generalist": ["claude", "codex", "grok", "agy"],
        "explorer": ["claude", "grok", "agy"],
        "planner": ["claude", "agy", "codex"],
        "coder": ["claude", "codex", "agy"],
        "committer": ["claude", "codex"],
        "tester": ["codex", "claude"],
        "reviewer": ["agy", "claude", "codex"],
        "evaluator": ["agy", "claude", "codex"],
        "researcher": ["grok", "claude", "agy"],
        "scout": ["grok", "codex", "claude"],
        "investigator": ["grok", "claude", "codex"],
        "responder": ["claude", "codex"],
        "orchestrator": ["claude", "agy"],
        "worker": ["codex", "claude", "agy", "grok"],
        "candidate": ["claude", "codex", "agy", "grok"],
        "judge": ["claude", "agy"],
        "skeptic": ["grok", "claude"],
        "writer": ["claude", "agy"],
        "grader": ["agy", "claude"],
    },
    "router": {"default_loop": "react", "min_score": 2},
    "history_trim": {
        "enabled": False,
        "budget_fill": 0.5,
        "write_recent_turns": 20,
        "result_keep_recent_turns": 8,
        "head_lines": 40,
        "tail_lines": 40,
        "max_tool_result_lines": 100,
        "keep_error_lines": True,
        "keep_recent_turns": 20,
    },
    "loops": LOOPS,
}

# Implementation-heavy stages prefer codex (implementation/tests), keeping claude for planning.
for loop in ("research_plan_implement", "spec_driven", "pdca"):
    for s in LOOPS[loop]["stages"]:
        if s["role"] == "coder":
            s["agents"] = ["codex", "claude", "agy"]

if __name__ == "__main__":
    text = json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n"
    p = ROOT / "src/looper/data/default_config.json"
    p.write_text(text, encoding="utf-8")
    print("wrote", p)
