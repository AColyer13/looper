import subprocess
import sys

from looper import detect
from looper.policy import AgentSpec
from looper.runners import Invocation, SubprocessRunner


def test_detect_missing_and_present(policy, monkeypatch, tmp_path):
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'claude 9.9.9'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    st = detect.detect_agents(policy)
    assert st["claude"].installed and st["claude"].version == "claude 9.9.9"
    assert not st["codex"].installed and st["codex"].version is None


def test_env_binary_override(policy, monkeypatch, tmp_path):
    fake = tmp_path / "my-grok"
    fake.write_text("#!/bin/sh\necho v1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("LOOPER_GROK_BIN", str(fake))
    st = detect.detect_agent(policy.agents["grok"], probe=False)
    assert st.installed and st.path == str(fake)


def test_subprocess_runner_real_process():
    agent = AgentSpec("py", sys.executable, ["{binary}", "-c", "import sys; print(sys.argv[1].upper())", "{prompt}"])
    res = SubprocessRunner()(Invocation(agent, sys.executable, "hello", timeout=10))
    assert res.ok and res.output == "HELLO"


def test_subprocess_runner_timeout_and_error():
    agent = AgentSpec("py", sys.executable, ["{binary}", "-c", "import time; time.sleep(5)", "{prompt}"])
    res = SubprocessRunner()(Invocation(agent, sys.executable, "x", timeout=0.3))
    assert not res.ok and res.timed_out
    bad = AgentSpec("py", sys.executable, ["{binary}", "-c", "import sys; sys.exit(3)", "{prompt}"])
    res = SubprocessRunner()(Invocation(bad, sys.executable, "x", timeout=10))
    assert not res.ok and res.returncode == 3


def test_subprocess_runner_stdin_mode():
    agent = AgentSpec("py", sys.executable, ["{binary}", "-c", "import sys; print(sys.stdin.read()[::-1])"],
                      prompt_via="stdin")
    res = SubprocessRunner()(Invocation(agent, sys.executable, "abc", timeout=10))
    assert res.output == "cba"


def test_prepare_cli_call_auto_switches_to_stdin():
    from looper.runners import PROMPT_ARGV_LIMIT, PromptTooLongError, prepare_cli_call, describe_call

    short = "hi"
    long = "x" * (PROMPT_ARGV_LIMIT + 50)
    arg_agent = AgentSpec("py", sys.executable, ["{binary}", "-c", "pass", "{prompt}"], prompt_via="arg")
    auto_agent = AgentSpec("py", sys.executable, ["{binary}", "-c", "import sys; print(len(sys.stdin.read()))"],
                           prompt_via="auto")
    stdin_agent = AgentSpec("py", sys.executable, ["{binary}", "-c", "import sys; print(len(sys.stdin.read()))"],
                            prompt_via="stdin")

    argv, use = prepare_cli_call(arg_agent, sys.executable, short)
    assert use is False and short in argv

    try:
        prepare_cli_call(arg_agent, sys.executable, long)
        assert False, "expected PromptTooLongError"
    except PromptTooLongError as exc:
        assert "prompt_via" in str(exc)

    argv, use = prepare_cli_call(auto_agent, sys.executable, short)
    assert use is False
    argv, use = prepare_cli_call(auto_agent, sys.executable, long)
    assert use is True and not any(long in a for a in argv)

    argv, use = prepare_cli_call(stdin_agent, sys.executable, short)
    assert use is True
    desc = describe_call(auto_agent, sys.executable, long)
    assert any(a.startswith("<stdin:") for a in desc)


def test_subprocess_runner_auto_long_prompt():
    from looper.runners import PROMPT_ARGV_LIMIT

    agent = AgentSpec(
        "py", sys.executable,
        ["{binary}", "-c", "import sys; print(len(sys.stdin.read()))"],
        prompt_via="auto",
    )
    big = "z" * (PROMPT_ARGV_LIMIT + 100)
    res = SubprocessRunner()(Invocation(agent, sys.executable, big, timeout=10))
    assert res.ok, res.error
    assert res.output == str(len(big))
