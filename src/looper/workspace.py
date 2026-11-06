"""Project and machine workspace: AGENTS.md, roles, skills, memory.

Discovery walks upward from the working directory until it finds a ``.looper``
directory. ``--project`` / the request ``project`` field selects the root
explicitly. With no ``.looper`` directory and no skills or MCP config, nothing
is injected and callers see today's prompts.

Skills use ``SKILL.md`` (``name`` + ``description`` frontmatter). Project skills
override machine skills of the same name. Only the name and description are
placed in the prompt; ``load_skill`` returns the body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from looper.config import ConfigError, state_dir
from looper.mcp_config import McpSettings, RolePolicy, ToolLists, load_mcp_settings

_MEMORY_CHAR_CAP = 8000
_FM_START = re.compile(r"^---\s*$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    scope: str  # "user" | "project"

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "path": str(self.path),
        }


@dataclass
class RoleDoc:
    name: str
    body: str
    path: Path
    model: str | None = None
    max_tool_calls: int | None = None
    policy: RolePolicy = field(default_factory=RolePolicy)


@dataclass
class Workspace:
    machine_dir: Path
    project_root: Path | None = None
    project_dir: Path | None = None
    agents_md: str | None = None
    agents_path: Path | None = None
    soul_md: str | None = None
    user_md: str | None = None
    memory_text: str | None = None
    roles: dict[str, RoleDoc] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    skills_enabled: bool = True
    mcp: McpSettings = field(default_factory=McpSettings)
    warnings: list[str] = field(default_factory=list)

    def role_doc(self, name: str | None, abstract: str | None = None) -> RoleDoc | None:
        if name and name in self.roles:
            return self.roles[name]
        if abstract and abstract in self.roles:
            return self.roles[abstract]
        return None

    def public(self) -> dict[str, Any]:
        machine_config = self.machine_dir / "looper.json"
        return {
            "machine_dir": str(self.machine_dir),
            "machine_config": str(machine_config),
            "machine_config_present": machine_config.is_file(),
            "project_root": str(self.project_root) if self.project_root else None,
            "project_dir": str(self.project_dir) if self.project_dir else None,
            "agents_md": self.agents_path is not None,
            "soul_md": self.soul_md is not None,
            "user_md": self.user_md is not None,
            "memory_md": self.memory_text is not None,
            "roles": sorted(self.roles),
        }

    def render(
        self,
        *,
        role: str | None = None,
        abstract_role: str | None = None,
        include_agents: bool = True,
        include_soul: bool = False,
        include_skills: bool = False,
    ) -> str:
        """System-prompt sections. Empty string when this workspace adds nothing."""
        parts: list[str] = []
        if include_soul and self.soul_md:
            parts.append(self.soul_md.strip())
        if self.user_md:
            parts.append("## User preferences\n" + self.user_md.strip())
        if self.memory_text:
            parts.append("## Memory\n" + self.memory_text.strip())
        if include_agents and self.agents_md:
            parts.append("## Operating rules\n" + self.agents_md.strip())
        doc = self.role_doc(role, abstract_role)
        if doc and doc.body.strip():
            parts.append(f"## Role: {doc.name}\n" + doc.body.strip())
        if include_skills and self.skills_enabled and self.skills:
            lines = [
                "## Skills",
                "These skills are available. Call the load_skill tool with the skill name "
                "to read its full instructions. Do not assume the body from the description.",
            ]
            for skill in self.skills.values():
                lines.append(f"- {skill.name}: {skill.description}")
            parts.append("\n".join(lines))
        return "\n\n".join(p for p in parts if p).strip()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``---`` block. Values are scalars or lists of scalars."""
    lines = text.splitlines()
    if not lines or not _FM_START.match(lines[0]):
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if _FM_START.match(lines[i]):
            end = i
            break
    if end is None:
        return {}, text
    meta = _parse_simple_block(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    if text in ("null", "~"):
        return None
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_list(inner)]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _split_list(inner: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _parse_simple_block(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " \t-" or ":" not in line:
            i += 1
            continue
        key, rest = line.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        i += 1
        if rest in ("", ">", "|"):
            items: list[Any] = []
            folded: list[str] = []
            while i < n and (not lines[i].strip() or lines[i][0] in " \t"):
                stripped = lines[i].strip()
                i += 1
                if not stripped:
                    continue
                if rest == "" and stripped.startswith("- "):
                    items.append(_parse_scalar(stripped[2:].strip()))
                else:
                    folded.append(stripped)
            if items and not folded:
                meta[key] = items
            elif rest == "|":
                meta[key] = "\n".join(folded)
            else:
                meta[key] = " ".join(folded)
            continue
        meta[key] = _parse_scalar(rest)
    return meta


def _string_list(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if value is True:
        return ["*"]
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return []


def _role_policy(meta: dict[str, Any]) -> RolePolicy:
    mode = meta.get("mcp_mode") or meta.get("mode")
    if mode is not None and mode not in ("auto_read_only", "approve_all", "deny_all"):
        mode = None
    return RolePolicy(
        mode=str(mode) if mode else None,
        lists=ToolLists(
            deny=_string_list(meta.get("deny")),
            require_approval=_string_list(meta.get("require_approval")),
            allow=_string_list(meta.get("allow")),
            auto_approve=_string_list(meta.get("auto_approve")),
        ),
    )


def find_project_root(start: Path | None = None, project: str | Path | None = None) -> Path | None:
    """Return the project root, or None when no ``.looper`` directory is found.

    ``project`` is an explicit root (the directory that contains ``.looper``,
    or the ``.looper`` directory itself).
    """
    if project:
        path = Path(project).expanduser()
        if not path.is_absolute():
            path = (Path(start) if start else Path.cwd()) / path
        path = path.resolve()
        if path.name == ".looper" and path.is_dir():
            return path.parent
        if not path.is_dir():
            raise ConfigError(f"project root not found: {path}")
        return path
    cur = Path(start).resolve() if start else Path.cwd().resolve()
    if cur.is_file():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        if (candidate / ".looper").is_dir():
            return candidate
        # Don't walk out of the filesystem root. parents already stops there.
        if candidate.parent == candidate:
            break
    return None


def _read(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _load_skill_file(path: Path, scope: str) -> Skill:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    name = meta.get("name") or path.parent.name
    if not isinstance(name, str) or not name.strip():
        name = path.parent.name
    description = meta.get("description") or ""
    if not isinstance(description, str):
        description = str(description)
    description = description.strip() or "(no description)"
    return Skill(name=name.strip(), description=description, body=body.strip(), path=path, scope=scope)


def _load_skills(root: Path, scope: str) -> dict[str, Skill]:
    skills_dir = root / "skills"
    found: dict[str, Skill] = {}
    if not skills_dir.is_dir():
        return found
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            skill = _load_skill_file(path, scope)
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        found[skill.name] = skill
    return found


def _load_roles(project_dir: Path | None) -> dict[str, RoleDoc]:
    if project_dir is None:
        return {}
    roles_dir = project_dir / "roles"
    if not roles_dir.is_dir():
        return {}
    out: dict[str, RoleDoc] = {}
    for path in sorted(roles_dir.glob("*.md")):
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        model = meta.get("model")
        max_calls = meta.get("max_tool_calls")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int):
            if isinstance(max_calls, str) and max_calls.isdigit():
                max_calls = int(max_calls)
            else:
                max_calls = None
        out[path.stem] = RoleDoc(
            name=path.stem,
            body=body,
            path=path,
            model=str(model) if isinstance(model, str) and model.strip() else None,
            max_tool_calls=max_calls if isinstance(max_calls, int) else None,
            policy=_role_policy(meta),
        )
    return out


def _load_memory(project_dir: Path) -> str | None:
    chunks: list[str] = []
    memory = _read(project_dir / "MEMORY.md")
    if memory and memory.strip():
        chunks.append(memory.strip())
    mem_dir = project_dir / "memory"
    if mem_dir.is_dir():
        for path in sorted(mem_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                chunks.append(f"### {path.name}\n{text}")
    if not chunks:
        return None
    joined = "\n\n".join(chunks)
    if len(joined) > _MEMORY_CHAR_CAP:
        joined = joined[:_MEMORY_CHAR_CAP].rstrip() + "\n…[memory truncated]"
    return joined


def _skills_enabled(policy_raw: dict[str, Any] | None, policy_path: Path | None) -> bool:
    enabled = True

    def apply(blob: dict[str, Any]) -> bool | None:
        skills = blob.get("skills")
        if skills is False:
            return False
        if isinstance(skills, dict) and "enabled" in skills:
            return bool(skills["enabled"])
        return None

    if isinstance(policy_raw, dict):
        flag = apply(policy_raw)
        if flag is not None:
            enabled = flag
    state_file = state_dir() / "looper.json"
    same = policy_path is not None and state_file.resolve() == Path(policy_path).resolve()
    if not same and state_file.is_file():
        import json
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{state_file}: invalid JSON ({exc})") from exc
        if isinstance(raw, dict):
            flag = apply(raw)
            if flag is not None:
                enabled = flag
    return enabled


def load_workspace(
    start: Path | str | None = None,
    project: str | Path | None = None,
    policy_raw: dict[str, Any] | None = None,
    policy_path: Path | None = None,
) -> Workspace:
    """Load the workspace. Missing directories are an empty workspace, not an error."""
    start_path = Path(start).expanduser() if start else Path.cwd()
    root = find_project_root(start_path, project)
    project_dir = (root / ".looper") if root is not None else None
    if project_dir is not None and not project_dir.is_dir():
        project_dir = None
    machine = state_dir()
    skills: dict[str, Skill] = {}
    enabled = _skills_enabled(policy_raw, policy_path)
    if enabled:
        if machine.is_dir():
            skills.update(_load_skills(machine, "user"))
        if project_dir is not None:
            skills.update(_load_skills(project_dir, "project"))
    agents_path = project_dir / "AGENTS.md" if project_dir else None
    ws = Workspace(
        machine_dir=machine,
        project_root=root,
        project_dir=project_dir,
        agents_md=_read(agents_path) if agents_path else None,
        agents_path=agents_path if agents_path and agents_path.is_file() else None,
        soul_md=_read(project_dir / "SOUL.md") if project_dir else None,
        user_md=_read(project_dir / "USER.md") if project_dir else None,
        memory_text=_load_memory(project_dir) if project_dir else None,
        roles=_load_roles(project_dir),
        skills=skills,
        skills_enabled=enabled,
        mcp=load_mcp_settings(policy_raw, policy_path, root),
    )
    if ws.soul_md is not None and not ws.soul_md.strip():
        ws.soul_md = None
    if ws.user_md is not None and not ws.user_md.strip():
        ws.user_md = None
    if ws.agents_md is not None and not ws.agents_md.strip():
        ws.agents_md = None
        ws.agents_path = None
    return ws


def load_skill_result(ws: Workspace, arguments: dict[str, Any] | None) -> tuple[bool, str]:
    """Execute the built-in ``load_skill`` tool. Returns ``(ok, text)``."""
    name = (arguments or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return False, "load_skill requires a string argument 'name'"
    skill = ws.skills.get(name.strip())
    if skill is None:
        known = ", ".join(ws.skills) or "(none)"
        return False, f"unknown skill {name!r}; known: {known}"
    return True, skill.body


def load_skill_spec():
    """Tool schema for ``load_skill``. Imported lazily to avoid a tools import cycle."""
    from looper.tools import ToolSpec
    return ToolSpec(
        name="load_skill",
        description=(
            "Load the full instructions for one skill by name. "
            "The system prompt lists skill names and descriptions only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from the skills list"},
            },
            "required": ["name"],
        },
    )
