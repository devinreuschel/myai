import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from myai.agentsync.config import RepoConfig, load_config, save_config
from myai.agentsync.master import Frontmatter, Rule, Skill, Subagent
from myai.agentsync.render import (
    BLOCK_HEADER,
    build_plan,
    render_claude_rule,
    render_cursor_rule,
)


def _rule(name: str, body: str = "rule body", **frontmatter: object) -> Rule:
    """Build a test rule with optional frontmatter."""
    fm = Frontmatter(raw=dict(frontmatter))
    return Rule(name=name, path=Path(f"/master/rules/{name}.md"), frontmatter=fm, body=body)


class TestBuildPlanNested(unittest.TestCase):
    """Nested rules mode (default)."""

    def test_cursor_only_writes_nested_files(self) -> None:
        """Cursor-only repos get nested .mdc files, no AGENTS.md block."""
        rules = [_rule("general/rule", description="General rule")]
        plan = build_plan(Path("/repo"), ["cursor"], rules, [], [], nested_rules=True)

        self.assertIn(".cursor/rules/general/rule.mdc", plan.files)
        self.assertNotIn("AGENTS.md", plan.blocks)
        self.assertIn("rule body", plan.files[".cursor/rules/general/rule.mdc"].content or "")

    def test_pi_only_writes_agents_md_block(self) -> None:
        """Pi-only repos flatten into AGENTS.md, no .cursor files."""
        rules = [_rule("general/rule", description="General rule")]
        plan = build_plan(Path("/repo"), ["pi"], rules, [], [], nested_rules=True)

        self.assertIn("AGENTS.md", plan.blocks)
        self.assertNotIn(".cursor/rules/general/rule.mdc", plan.files)
        self.assertIn("## general/rule", plan.blocks["AGENTS.md"])
        self.assertIn("General rule", plan.blocks["AGENTS.md"])

    def test_cursor_and_pi_nested_writes_both(self) -> None:
        """Cursor+pi with nested rules writes both nested files and AGENTS.md."""
        rules = [_rule("general/rule")]
        plan = build_plan(
            Path("/repo"),
            ["cursor", "pi"],
            rules,
            [],
            [],
            nested_rules=True,
        )

        self.assertIn(".cursor/rules/general/rule.mdc", plan.files)
        self.assertIn("AGENTS.md", plan.blocks)
        self.assertIn("## general/rule", plan.blocks["AGENTS.md"])

    def test_claude_nested_writes_claude_rules(self) -> None:
        """Claude nested mode writes .claude/rules/*.md files."""
        rules = [_rule("api/rules", description="API rules", globs=["src/api/**"])]
        plan = build_plan(Path("/repo"), ["claude"], rules, [], [], nested_rules=True)

        self.assertIn(".claude/rules/api/rules.md", plan.files)
        self.assertNotIn("CLAUDE.md", plan.blocks)
        content = plan.files[".claude/rules/api/rules.md"].content or ""
        self.assertIn("globs: src/api/**", content)
        self.assertIn("alwaysApply: false", content)


class TestBuildPlanFlat(unittest.TestCase):
    """Flat rules mode."""

    def test_cursor_flattens_to_agents_md(self) -> None:
        """Cursor with nested_rules=false flattens into AGENTS.md."""
        rules = [_rule("general/rule")]
        plan = build_plan(Path("/repo"), ["cursor"], rules, [], [], nested_rules=False)

        self.assertNotIn(".cursor/rules/general/rule.mdc", plan.files)
        self.assertIn("AGENTS.md", plan.blocks)
        self.assertIn("## general/rule", plan.blocks["AGENTS.md"])

    def test_claude_flattens_to_claude_md(self) -> None:
        """Claude with nested_rules=false flattens into CLAUDE.md."""
        rules = [_rule("general/rule")]
        plan = build_plan(Path("/repo"), ["claude"], rules, [], [], nested_rules=False)

        self.assertNotIn(".claude/rules/general/rule.md", plan.files)
        self.assertIn("CLAUDE.md", plan.blocks)
        self.assertIn("## general/rule", plan.blocks["CLAUDE.md"])

    def test_cursor_and_pi_flat_merge_into_agents_md(self) -> None:
        """Cursor-flat and pi merge into one AGENTS.md block, deduped by name."""
        rules = [
            _rule("shared"),
            _rule("cursor-only", agents=["cursor"]),
            _rule("pi-only", agents=["pi"]),
        ]
        plan = build_plan(
            Path("/repo"),
            ["cursor", "pi"],
            rules,
            [],
            [],
            nested_rules=False,
        )

        self.assertNotIn(".cursor/rules/shared.mdc", plan.files)
        block = plan.blocks["AGENTS.md"]
        self.assertIn("## shared", block)
        self.assertIn("## cursor-only", block)
        self.assertIn("## pi-only", block)
        self.assertEqual(block.count("## shared"), 1)


class TestRenderClaudeRule(unittest.TestCase):
    """Claude nested rule rendering."""

    def test_renders_frontmatter(self) -> None:
        """Claude rules emit globs as unquoted CSV and alwaysApply."""
        rule = _rule(
            "api/rules",
            body="Use REST conventions.",
            description="API rules",
            globs=["src/api/**", "lib/api/**"],
            alwaysApply=False,
        )
        content = render_claude_rule(rule)

        self.assertIn("description: API rules", content)
        self.assertIn("globs: src/api/**, lib/api/**", content)
        self.assertIn("alwaysApply: false", content)
        self.assertIn("Use REST conventions.", content)


class TestRenderCursorRule(unittest.TestCase):
    """Cursor nested rule rendering."""

    def test_renders_globs_list(self) -> None:
        """Cursor rules keep globs as a YAML list."""
        rule = _rule("ts", globs=["**/*.ts"], alwaysApply=True)
        content = render_cursor_rule(rule)

        self.assertIn("globs: ['**/*.ts']", content)
        self.assertIn("alwaysApply: true", content)


class TestBuildPlanSkillsAndSubagents(unittest.TestCase):
    """Skills and subagents still render alongside rules."""

    def test_skills_written_for_enabled_agents(self) -> None:
        """Skills land under each enabled agent's skills dir."""
        skill = Skill(name="deploy", path=Path("/master/skills/deploy"))
        plan = build_plan(Path("/repo"), ["cursor", "pi"], [], [skill], [], nested_rules=True)

        self.assertIn(".cursor/skills/deploy", plan.files)
        self.assertIn(".pi/skills/deploy", plan.files)

    def test_subagents_claude_only(self) -> None:
        """Subagents render only for claude."""
        sub = Subagent(
            name="reviewer",
            path=Path("/master/subagents/reviewer.md"),
            frontmatter=Frontmatter(raw={"description": "Code reviewer"}),
            body="Review carefully.",
        )
        plan = build_plan(Path("/repo"), ["claude"], [], [], [sub], nested_rules=True)

        self.assertIn(".claude/agents/reviewer.md", plan.files)
        content = plan.files[".claude/agents/reviewer.md"].content or ""
        self.assertIn("Review carefully.", content)


class TestRepoConfigNestedRules(unittest.TestCase):
    """Config load/save for nested_rules."""

    def test_defaults_to_true(self) -> None:
        """Legacy configs without nested_rules default to true."""
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg_path = repo / ".myai" / "config.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({"agents": ["cursor"], "rules": ["general"]}),
                encoding="utf-8",
            )
            cfg = load_config(repo)
            self.assertTrue(cfg.nested_rules)

    def test_round_trip(self) -> None:
        """nested_rules survives save/load."""
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            save_config(
                repo,
                RepoConfig(agents=["pi"], rules=["general"], nested_rules=False),
            )
            cfg = load_config(repo)
            self.assertFalse(cfg.nested_rules)

    def test_managed_block_header(self) -> None:
        """Flattened blocks include the myai managed header."""
        rules = [_rule("general/rule")]
        plan = build_plan(Path("/repo"), ["pi"], rules, [], [], nested_rules=False)
        block = plan.blocks["AGENTS.md"]

        self.assertIn(BLOCK_HEADER, block)


if __name__ == "__main__":
    unittest.main()
