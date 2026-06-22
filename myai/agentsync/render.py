import hashlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from myai.agentsync.master import Rule, Skill, Subagent, filter_rules_for_agent

BLOCK_BEGIN = "<!-- myai:begin -->"
BLOCK_END = "<!-- myai:end -->"
BLOCK_HEADER = "<!-- managed by myai; edit rules in the master repo -->"


@dataclass
class RenderedFile:
    """A file to write or a skill directory to copy during sync."""

    rel_path: str
    content: str | None = None  # None = directory tree copy from source
    source_dir: Path | None = None


@dataclass
class RenderPlan:
    """Planned file writes and managed-block injections for one repo."""

    files: dict[str, RenderedFile] = field(default_factory=dict)
    blocks: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentCaps:
    """Per-agent rendering capabilities."""

    supports_nested: bool
    flat_target: str
    nested_dir: str | None
    nested_ext: str | None
    skills_dir: str | None
    render_nested: Callable[[Rule], str] | None


def sha256_text(text: str) -> str:
    """Return a sha256: prefix hash of text."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """Return a sha256: prefix hash of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def render_cursor_rule(rule: Rule) -> str:
    """Render a rule as a Cursor .mdc file."""
    fm = rule.frontmatter
    lines = ["---"]
    desc = fm.get_str("description")
    if desc:
        lines.append(f"description: {desc}")
    globs = fm.get_list("globs")
    if globs:
        lines.append(f"globs: {globs}")
    always = fm.get_bool("alwaysApply")
    lines.append(f"alwaysApply: {str(always).lower()}")
    lines.append("---")
    lines.append("")
    if rule.body:
        lines.append(rule.body)
    return "\n".join(lines).rstrip() + "\n"


def render_claude_rule(rule: Rule) -> str:
    """Render a rule as a Claude Code .claude/rules/*.md file."""
    fm = rule.frontmatter
    lines = ["---"]
    desc = fm.get_str("description")
    if desc:
        lines.append(f"description: {desc}")
    globs = fm.get_list("globs")
    if globs:
        lines.append(f"globs: {', '.join(globs)}")
    always = fm.get_bool("alwaysApply")
    lines.append(f"alwaysApply: {str(always).lower()}")
    lines.append("---")
    lines.append("")
    if rule.body:
        lines.append(rule.body)
    return "\n".join(lines).rstrip() + "\n"


AGENT_CAPS: dict[str, AgentCaps] = {
    "cursor": AgentCaps(
        supports_nested=True,
        flat_target="AGENTS.md",
        nested_dir=".cursor/rules",
        nested_ext=".mdc",
        skills_dir=".cursor/skills",
        render_nested=render_cursor_rule,
    ),
    "claude": AgentCaps(
        supports_nested=True,
        flat_target="CLAUDE.md",
        nested_dir=".claude/rules",
        nested_ext=".md",
        skills_dir=".claude/skills",
        render_nested=render_claude_rule,
    ),
    "pi": AgentCaps(
        supports_nested=False,
        flat_target="AGENTS.md",
        nested_dir=None,
        nested_ext=None,
        skills_dir=".pi/skills",
        render_nested=None,
    ),
}


def render_rules_block(rules: list[Rule], title: str) -> str:
    """Flatten rules into a managed markdown block."""
    if not rules:
        return ""
    parts = [BLOCK_HEADER, "", f"# {title}", ""]
    for rule in rules:
        parts.append(f"## {rule.name}")
        desc = rule.frontmatter.get_str("description")
        if desc:
            parts.append(f"*{desc}*")
            parts.append("")
        if rule.body:
            parts.append(rule.body)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def inject_block(existing: str, block_content: str) -> str:
    """Replace or remove the myai managed block in existing file content."""
    pattern = re.compile(
        rf"{re.escape(BLOCK_BEGIN)}.*?{re.escape(BLOCK_END)}\n?",
        re.DOTALL,
    )
    cleaned = pattern.sub("", existing)
    if not block_content.strip():
        return cleaned.rstrip() + ("\n" if cleaned else "")
    wrapped = f"{BLOCK_BEGIN}\n{block_content.rstrip()}\n{BLOCK_END}\n"
    if cleaned.strip():
        return cleaned.rstrip() + "\n\n" + wrapped
    return wrapped


def render_subagent(rule: Subagent) -> str:
    """Render a subagent definition for Claude."""
    fm = rule.frontmatter
    lines = ["---"]
    name = fm.get_str("name") or rule.name
    lines.append(f"name: {name}")
    desc = fm.get_str("description")
    if desc:
        lines.append(f"description: {desc}")
    tools = fm.get_list("tools")
    if tools:
        lines.append(f"tools: {tools}")
    model = fm.get_str("model")
    if model:
        lines.append(f"model: {model}")
    lines.append("---")
    lines.append("")
    if rule.body:
        lines.append(rule.body)
    return "\n".join(lines).rstrip() + "\n"


def _merge_rules(existing: list[Rule], incoming: list[Rule]) -> list[Rule]:
    """Merge rule lists, deduping by rule.name."""
    seen = {r.name for r in existing}
    merged = list(existing)
    for rule in incoming:
        if rule.name not in seen:
            seen.add(rule.name)
            merged.append(rule)
    return merged


def _add_nested_rules(
    plan: RenderPlan,
    rules: list[Rule],
    caps: AgentCaps,
) -> None:
    """Write nested rule files for an agent."""
    if caps.nested_dir is None or caps.nested_ext is None or caps.render_nested is None:
        return
    for rule in rules:
        rel = f"{caps.nested_dir}/{rule.name}{caps.nested_ext}"
        plan.files[rel] = RenderedFile(
            rel_path=rel,
            content=caps.render_nested(rule),
        )


def build_plan(
    repo: Path,
    agents: list[str],
    rules: list[Rule],
    skills: list[Skill],
    subagents: list[Subagent],
    nested_rules: bool = True,
) -> RenderPlan:
    """Build the render plan for a repo sync."""
    del repo  # reserved for future repo-specific rendering
    plan = RenderPlan()
    flat_rules: dict[str, list[Rule]] = {}

    for agent in agents:
        caps = AGENT_CAPS.get(agent)
        if caps is None:
            continue

        agent_rules = filter_rules_for_agent(rules, agent)
        use_nested = caps.supports_nested and nested_rules

        if use_nested:
            _add_nested_rules(plan, agent_rules, caps)
        else:
            flat_rules[caps.flat_target] = _merge_rules(
                flat_rules.get(caps.flat_target, []),
                agent_rules,
            )

        if caps.skills_dir:
            for skill in skills:
                rel = f"{caps.skills_dir}/{skill.name}"
                plan.files[rel] = RenderedFile(
                    rel_path=rel,
                    source_dir=skill.path,
                )

    if "claude" in agents:
        for sub in subagents:
            rel = f".claude/agents/{sub.name}.md"
            plan.files[rel] = RenderedFile(rel_path=rel, content=render_subagent(sub))

    for target, target_rules in flat_rules.items():
        block = render_rules_block(target_rules, "Project rules (myai)")
        if block:
            plan.blocks[target] = block

    return plan


def copy_skill_dir(src: Path, dst: Path) -> None:
    """Copy a skill directory tree, replacing any existing destination."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def collect_skill_files(skill_dir: Path, rel_prefix: str) -> dict[str, str]:
    """Return rel_path -> sha256 for all files under a skill directory."""
    result: dict[str, str] = {}
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            rel = f"{rel_prefix}/{path.relative_to(skill_dir).as_posix()}"
            result[rel] = sha256_file(path)
    return result
