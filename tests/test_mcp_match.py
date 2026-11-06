"""Word, verb, and regex policy entries (mcp_match_v2). Globs stay fnmatchcase."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looper import api
from looper.cli import app
from looper.config import ConfigError
from looper.mcp_config import ServerSpec, decide, entry_matches, tool_tokens, tool_visible, validate_entry
from looper.policy import load_policy
from looper.workspace import load_workspace

runner = CliRunner()

# One verb line the frontend can drop in. query is included on purpose:
# search_query must not match the leading-verb form.
VERBS = (
    "verb:delete,remove,drop,purge,truncate,destroy,trash,archive,clear,reset,"
    "revoke,unlink,close,cancel,pay,charge,refund,transfer,purchase,grant,assign,"
    "approve,invite,deploy,create,add,insert,new,post,put,upload,save,set,append,"
    "write,edit,update,modify,patch,rename,move,replace,upsert,merge,commit,push,"
    "apply,sync,send,reply,forward,publish,share,comment,react,notify,email,"
    "message,tweet,run,exec,execute,shell,eval,query,trigger,start,stop,restart,"
    "kill,install"
)


@pytest.mark.parametrize(
    "server,tool,entry",
    [
        ("gh", "create_issue", "verb:create"),
        ("gh", "sendMessage", "verb:send"),
        ("git", "git_push", "word:push"),
        ("db", "drop_table", "verb:drop"),
        ("sh", "run_command", "verb:run"),
        ("ops", "deploy-app", "verb:deploy"),
        ("notes", "write_note", "notes:verb:write"),
        ("gh", "CreateIssue", "verb:create"),
        ("mail", "sendmail", "verb:send"),
    ],
)
def test_match_positives(server, tool, entry):
    assert entry_matches(entry, server, tool)


@pytest.mark.parametrize(
    "tool",
    [
        "get_settings",
        "get_posts",
        "list_comments",
        "read_message",
        "search_issues",
        "fetch_dataset",
        "search_query",
    ],
)
def test_leading_verb_negatives(tool):
    assert not entry_matches(VERBS, "srv", tool)


def test_word_matches_any_token_verb_is_leading_only():
    assert entry_matches("word:push", "git", "git_push")
    assert not entry_matches("verb:push", "git", "git_push")
    assert entry_matches("word:query", "search", "search_query")
    assert not entry_matches("verb:query", "search", "search_query")
    # whole tokens: post does not match posts, set does not match settings
    assert not entry_matches("verb:post", "gh", "get_posts")
    assert not entry_matches("word:post", "gh", "get_posts")
    assert not entry_matches("verb:set", "cfg", "settings")
    assert not entry_matches("word:set", "cfg", "get_settings")


def test_server_scope_and_plain_globs_unchanged():
    assert entry_matches("notes:verb:write", "notes", "write_note")
    assert not entry_matches("other:verb:write", "notes", "write_note")
    assert entry_matches("read_*", "notes", "read_note")
    assert not entry_matches("read_*", "notes", "write_note")
    assert entry_matches("notes:read_*", "notes", "read_note")
    assert not entry_matches("notes:read_*", "other", "read_note")
    # fnmatchcase stays case-sensitive
    assert entry_matches("CreateIssue", "gh", "CreateIssue")
    assert not entry_matches("createissue", "gh", "CreateIssue")
    assert entry_matches("re:(?i)^create", "gh", "CreateIssue")
    assert entry_matches("re:(?i)send", "mail", "sendmail")


def test_tokens_cover_case_styles():
    assert tool_tokens("create_issue") == ("create", "issue")
    assert tool_tokens("sendMessage") == ("send", "message")
    assert tool_tokens("CreateIssue") == ("create", "issue")
    assert tool_tokens("deploy-app") == ("deploy", "app")
    assert tool_tokens("git_push") == ("git", "push")
    assert tool_tokens("sendmail") == ("sendmail",)
    assert tool_tokens("search_query") == ("search", "query")


def test_include_filter_accepts_verb_form():
    server = ServerSpec(
        name="notes", transport="stdio", source="test", command="python",
        include=["verb:read"],
    )
    assert tool_visible(server, "read_note")
    assert not tool_visible(server, "write_note")


def test_role_frontmatter_verb_line_is_one_entry(tmp_path):
    root = tmp_path / "repo"
    role = root / ".looper" / "roles"
    role.mkdir(parents=True)
    (role / "coder.md").write_text(
        "---\n"
        "deny:\n"
        "  - verb:drop,deploy\n"
        "  - verb:query\n"
        "---\n\n"
        "Be careful.\n",
        encoding="utf-8",
    )
    ws = load_workspace(start=root, project=root)
    policy = ws.role_doc("coder").policy
    assert policy.lists.deny == ["verb:drop,deploy", "verb:query"]
    server = ServerSpec(name="db", transport="stdio", source="test", command="x")
    assert decide(ws.mcp, server, "drop_table", {}, policy)[0] == "deny"
    assert decide(ws.mcp, server, "deploy-app", {}, policy)[0] == "deny"
    # leading verb is search, not query
    assert decide(ws.mcp, server, "search_query", {"readOnlyHint": True}, policy)[0] == "auto"
    assert decide(ws.mcp, server, "get_settings", {"readOnlyHint": True}, policy)[0] == "auto"


def test_bad_regex_names_the_mcp_file_and_doctor_reports_it(tmp_path):
    root = tmp_path / "repo"
    mcp = root / ".looper" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({
        "mcpServers": {"notes": {"command": "python", "deny": ["re:("]}},
    }))
    with pytest.raises(ConfigError, match=r"invalid regex") as caught:
        load_workspace(start=root, project=root)
    text = str(caught.value)
    assert "mcp.json" in text
    assert "re:(" in text
    pol = load_policy()
    doc = api.doctor(pol, probe=False, project=root)
    blob = "\n".join(doc["problems"])
    assert "invalid regex" in blob
    assert "mcp.json" in blob
    assert "re:(" in blob


def test_bad_regex_in_role_frontmatter_names_the_file(tmp_path):
    root = tmp_path / "repo"
    path = root / ".looper" / "roles" / "coder.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ndeny:\n  - re:[\n---\n\nnope\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"invalid regex") as caught:
        load_workspace(start=root, project=root)
    text = str(caught.value)
    assert str(path) in text
    assert "re:[" in text
    doc = api.doctor(load_policy(), probe=False, project=root)
    blob = "\n".join(doc["problems"])
    assert "coder.md" in blob and "re:[" in blob


def test_pathological_regex_is_rejected():
    with pytest.raises(ConfigError, match="nested quantifiers"):
        validate_entry("re:(a+)+", "demo")
    with pytest.raises(ConfigError, match="needs at least one word"):
        validate_entry("verb:", "demo")


def test_version_advertises_mcp_match_v2():
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["version"] == "0.7.1"
    assert "mcp_match_v2" in data["capabilities"]
