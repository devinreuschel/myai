import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from myai.agentsync.master import Rule, Skill, Subagent, filter_rules_for_agent

BLOCK_BEGIN = "<!-- myai:begin -->"
BLOCK_END = "<!-- myai:end -->"
BLOCK_HEADER = "<!-- managed by myai; edit rules in the master repo -->"


@dataclass
class RenderedFile:
    rel_path: str
    content: str | None = None  # None = directory tree copy from source
    source_dir: Path | None = None


@dataclass
class RenderPlan:
    files: dict[str, RenderedFile] = field(default_factory=dict)
    blocks: dict[str, str] = field(default_factory=dict)


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def render_cursor_rule(rule: Rule) -> str:
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


def render_rules_block(rules: list[Rule], title: str) -> str:
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


def build_plan(
    repo: Path,
    agents: list[str],
    rules: list[Rule],
    skills: list[Skill],
    subagents: list[Subagent],
) -> RenderPlan:
    plan = RenderPlan()

    if "cursor" in agents:
        cursor_rules = filter_rules_for_agent(rules, "cursor")
        for rule in cursor_rules:
            rel = f".cursor/rules/{rule.name}.mdc"
            plan.files[rel] = RenderedFile(rel_path=rel, content=render_cursor_rule(rule))
        for skill in skills:
            rel = f".cursor/skills/{skill.name}"
            plan.files[rel] = RenderedFile(
                rel_path=rel,
                source_dir=skill.path,
            )

    if "claude" in agents:
        claude_rules = filter_rules_for_agent(rules, "claude")
        block = render_rules_block(claude_rules, "Project rules (myai)")
        if block:
            plan.blocks["CLAUDE.md"] = block
        for skill in skills:
            rel = f".claude/skills/{skill.name}"
            plan.files[rel] = RenderedFile(rel_path=rel, source_dir=skill.path)
        for sub in subagents:
            rel = f".claude/agents/{sub.name}.md"
            plan.files[rel] = RenderedFile(rel_path=rel, content=render_subagent(sub))

    if "pi" in agents:
        pi_rules = filter_rules_for_agent(rules, "pi")
        block = render_rules_block(pi_rules, "Project rules (myai)")
        if block:
            plan.blocks["AGENTS.md"] = block
        for skill in skills:
            rel = f".pi/skills/{skill.name}"
            plan.files[rel] = RenderedFile(rel_path=rel, source_dir=skill.path)

    return plan


def copy_skill_dir(src: Path, dst: Path) -> None:
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
