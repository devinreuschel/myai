import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from myai.agentsync.master import Frontmatter, Rule, Skill, Subagent, filter_rules_for_agent
from myai.agentsync.render import (
    RenderedFile,
    render_claude_rule,
    render_rules_block,
    render_subagent,
)

GLOBAL_MYAI_MANAGED_RULE_BODY = """\
User-global agent rules, skills, and subagents are managed by myai and synced
from a master repo into your agent home directories (~/.claude, ~/.cursor,
~/.pi/agent). Do NOT edit them directly here. myai global sync overwrites them,
so local edits are lost.

If the user wants to change a rule, skill, or subagent: don't touch the managed
files. Explain they're myai-managed and must change in the master repo, then walk
them through it: edit the source under the master's rules/, skills/, or
subagents/, then run `myai global sync`. The master repo path is in the myai
registry.
"""

GLOBAL_MYAI_MANAGED_RULE = f"# myai-managed resources\n\n{GLOBAL_MYAI_MANAGED_RULE_BODY}"

CURSOR_RULES_WARNING = (
    "warning: cursor has no file-backed global rules; "
    "skipping rules for cursor (skills still sync to ~/.cursor/skills)"
)


@dataclass
class GlobalHomeCaps:
    """Rendering capabilities relative to an agent home root."""

    supports_nested: bool
    flat_target: str | None
    nested_dir: str | None
    nested_ext: str | None
    skills_dir: str
    render_nested: Callable[[Rule], str] | None
    supports_rules: bool = True
    supports_guardrail: bool = True
    supports_subagents: bool = False
    append_system_rel: str | None = None


GLOBAL_HOME_CAPS: dict[str, GlobalHomeCaps] = {
    "claude": GlobalHomeCaps(
        supports_nested=True,
        flat_target="CLAUDE.md",
        nested_dir="rules",
        nested_ext=".md",
        skills_dir="skills",
        render_nested=render_claude_rule,
        supports_subagents=True,
    ),
    "cursor": GlobalHomeCaps(
        supports_nested=False,
        flat_target=None,
        nested_dir=None,
        nested_ext=None,
        skills_dir="skills",
        render_nested=None,
        supports_rules=False,
        supports_guardrail=False,
    ),
    "pi": GlobalHomeCaps(
        supports_nested=False,
        flat_target="AGENTS.md",
        nested_dir=None,
        nested_ext=None,
        skills_dir="skills",
        render_nested=None,
        append_system_rel="APPEND_SYSTEM.md",
    ),
}


@dataclass
class GlobalRenderPlan:
    """Planned writes keyed as agent:relpath (relative to that agent's home)."""

    files: dict[str, RenderedFile] = field(default_factory=dict)
    blocks: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def agent_home(agent: str) -> Path:
    """Return the user-global config root for an agent."""
    if agent == "claude":
        if env := os.environ.get("CLAUDE_CONFIG_DIR"):
            return Path(env).expanduser().resolve()
        return Path.home() / ".claude"
    if agent == "cursor":
        return Path.home() / ".cursor"
    if agent == "pi":
        if env := os.environ.get("PI_CODING_AGENT_DIR"):
            return Path(env).expanduser().resolve()
        return Path.home() / ".pi" / "agent"
    raise ValueError(f"unknown agent {agent!r}")


def _state_key(agent: str, rel: str) -> str:
    return f"{agent}:{rel}"


def _myai_guardrail_rule() -> Rule:
    fm = Frontmatter(
        raw={
            "description": "myai-managed global resources; do not edit synced files directly",
            "alwaysApply": True,
        }
    )
    return Rule(
        name="myai-managed",
        path=Path("myai-managed"),
        frontmatter=fm,
        body=GLOBAL_MYAI_MANAGED_RULE_BODY,
    )


def _add_nested_rules(
    plan: GlobalRenderPlan,
    agent: str,
    rules: list[Rule],
    caps: GlobalHomeCaps,
) -> None:
    if caps.nested_dir is None or caps.nested_ext is None or caps.render_nested is None:
        return
    for rule in rules:
        rel = f"{caps.nested_dir}/{rule.name}{caps.nested_ext}"
        key = _state_key(agent, rel)
        plan.files[key] = RenderedFile(
            rel_path=rel,
            content=caps.render_nested(rule),
        )


def build_global_plan(
    agents: list[str],
    rules: list[Rule],
    skills: list[Skill],
    subagents: list[Subagent],
    nested_rules: bool = True,
    inject_myai_rule: bool = False,
) -> GlobalRenderPlan:
    """Build the render plan for user-home agent sync."""
    plan = GlobalRenderPlan()
    guardrail = _myai_guardrail_rule() if inject_myai_rule else None

    if "cursor" in agents and rules:
        plan.warnings.append(CURSOR_RULES_WARNING)

    for agent in agents:
        caps = GLOBAL_HOME_CAPS.get(agent)
        if caps is None:
            continue

        for skill in skills:
            rel = f"{caps.skills_dir}/{skill.name}"
            key = _state_key(agent, rel)
            plan.files[key] = RenderedFile(rel_path=rel, source_dir=skill.path)

        if caps.supports_subagents:
            for sub in subagents:
                rel = f"agents/{sub.name}.md"
                key = _state_key(agent, rel)
                plan.files[key] = RenderedFile(
                    rel_path=rel,
                    content=render_subagent(sub),
                )

        if inject_myai_rule and caps.append_system_rel:
            rel = caps.append_system_rel
            plan.files[_state_key(agent, rel)] = RenderedFile(
                rel_path=rel,
                content=GLOBAL_MYAI_MANAGED_RULE,
            )

        if not caps.supports_rules:
            continue

        agent_rules = filter_rules_for_agent(rules, agent)
        if guardrail is not None and caps.supports_guardrail and agent != "pi":
            agent_rules = [*agent_rules, guardrail]

        use_nested = caps.supports_nested and nested_rules
        if use_nested:
            _add_nested_rules(plan, agent, agent_rules, caps)
        elif caps.flat_target and agent_rules:
            block = render_rules_block(agent_rules, "Global rules (myai)")
            if block:
                plan.blocks[_state_key(agent, caps.flat_target)] = block

    return plan
