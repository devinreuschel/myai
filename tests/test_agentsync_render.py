import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.agentsync.config import (
    RepoConfig,
    RepoState,
    load_config,
    load_state,
    resolve_inject_myai_rule,
    save_config,
)
from myai.agentsync.master import Frontmatter, Rule, Skill, Subagent
from myai.agentsync.registry import set_master
from myai.agentsync.render import (
    BLOCK_HEADER,
    MYAI_APPEND_SYSTEM_REL,
    MYAI_MANAGED_RULE,
    build_plan,
    render_claude_rule,
    render_cursor_rule,
)
from myai.agentsync.sync import UnsafePathError, apply_sync, compute_sync, sync_repo
from myai.global_config import (
    get_inject_myai_rule_default,
    load_global_config,
    save_global_config,
    set_inject_myai_rule_default,
    GlobalConfig,
)
from myai.paths import global_config_path


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


class TestMyaiManagedRule(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._config_dir = Path(self._tmp.name) / ".myai"
        self._config_dir.mkdir()
        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = self._tmp.name
        self._patch = patch("myai.paths.global_myai_dir", return_value=self._config_dir)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home

    def test_cursor_nested_injects_guardrail_rule(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["cursor"],
            [],
            [],
            [],
            nested_rules=True,
            inject_myai_rule=True,
        )
        rel = ".cursor/rules/myai-managed.mdc"
        self.assertIn(rel, plan.files)
        content = plan.files[rel].content or ""
        self.assertIn("alwaysApply: true", content)
        self.assertIn("myai sync overwrites them", content)
        self.assertNotIn(MYAI_APPEND_SYSTEM_REL, plan.files)

    def test_claude_nested_injects_guardrail_rule(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["claude"],
            [],
            [],
            [],
            nested_rules=True,
            inject_myai_rule=True,
        )
        rel = ".claude/rules/myai-managed.md"
        self.assertIn(rel, plan.files)
        self.assertIn("alwaysApply: true", plan.files[rel].content or "")

    def test_cursor_flat_injects_guardrail_in_agents_md(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["cursor"],
            [],
            [],
            [],
            nested_rules=False,
            inject_myai_rule=True,
        )
        self.assertIn("AGENTS.md", plan.blocks)
        self.assertIn("## myai-managed", plan.blocks["AGENTS.md"])

    def test_claude_flat_injects_guardrail_in_claude_md(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["claude"],
            [],
            [],
            [],
            nested_rules=False,
            inject_myai_rule=True,
        )
        self.assertIn("CLAUDE.md", plan.blocks)
        self.assertIn("## myai-managed", plan.blocks["CLAUDE.md"])

    def test_pi_only_guardrail_in_append_system_not_agents_md(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["pi"],
            [],
            [],
            [],
            inject_myai_rule=True,
        )
        self.assertIn(MYAI_APPEND_SYSTEM_REL, plan.files)
        self.assertNotIn("AGENTS.md", plan.blocks)

    def test_inject_myai_rule_off_skips_all_guardrails(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["cursor", "claude", "pi"],
            [],
            [],
            [],
            inject_myai_rule=False,
        )
        self.assertNotIn(MYAI_APPEND_SYSTEM_REL, plan.files)
        self.assertNotIn(".cursor/rules/myai-managed.mdc", plan.files)
        self.assertNotIn(".claude/rules/myai-managed.md", plan.files)
        self.assertNotIn("AGENTS.md", plan.blocks)
        self.assertNotIn("CLAUDE.md", plan.blocks)

    def test_pi_with_inject_myai_rule_writes_append_system(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["pi"],
            [],
            [],
            [],
            inject_myai_rule=True,
        )
        self.assertIn(MYAI_APPEND_SYSTEM_REL, plan.files)
        self.assertEqual(plan.files[MYAI_APPEND_SYSTEM_REL].content, MYAI_MANAGED_RULE)

    def test_cursor_only_no_append_system(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["cursor"],
            [],
            [],
            [],
            inject_myai_rule=True,
        )
        self.assertNotIn(MYAI_APPEND_SYSTEM_REL, plan.files)

    def test_inject_myai_rule_off_skips_append_system(self) -> None:
        plan = build_plan(
            Path("/repo"),
            ["pi"],
            [],
            [],
            [],
            inject_myai_rule=False,
        )
        self.assertNotIn(MYAI_APPEND_SYSTEM_REL, plan.files)

    def test_inject_myai_rule_config_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            save_config(repo, RepoConfig(agents=["pi"], inject_myai_rule=False))
            cfg = load_config(repo)
            self.assertFalse(cfg.inject_myai_rule)

    def test_resolve_inject_myai_rule_uses_per_repo_override(self) -> None:
        set_inject_myai_rule_default(True)
        cfg = RepoConfig(inject_myai_rule=False)
        self.assertFalse(resolve_inject_myai_rule(cfg))

    def test_resolve_inject_myai_rule_inherits_global_default(self) -> None:
        set_inject_myai_rule_default(False)
        cfg = RepoConfig(agents=["pi"])
        self.assertFalse(resolve_inject_myai_rule(cfg))
        set_inject_myai_rule_default(True)
        self.assertTrue(resolve_inject_myai_rule(cfg))

    def test_global_default_absent_is_true(self) -> None:
        self.assertTrue(get_inject_myai_rule_default())

    def test_sync_prunes_append_system_when_toggled_off(self) -> None:
        with TemporaryDirectory() as tmp:
            master = Path(tmp) / "master"
            master.mkdir()
            rules_dir = master / "rules"
            rules_dir.mkdir()
            (rules_dir / "general.md").write_text("General rule\n", encoding="utf-8")
            set_master(master)

            repo = Path(tmp) / "repo"
            repo.mkdir()
            save_config(repo, RepoConfig(agents=["pi"], rules=["general"]))
            apply_sync(
                repo,
                compute_sync(repo, load_config(repo), RepoState()),
                RepoState(),
            )
            self.assertTrue((repo / MYAI_APPEND_SYSTEM_REL).is_file())

            save_config(
                repo,
                RepoConfig(agents=["pi"], rules=["general"], inject_myai_rule=False),
            )
            cfg = load_config(repo)
            old_state = load_state(repo)
            sync_plan = compute_sync(repo, cfg, old_state)
            apply_sync(repo, sync_plan, old_state)
            self.assertFalse((repo / MYAI_APPEND_SYSTEM_REL).exists())
            self.assertNotIn(MYAI_APPEND_SYSTEM_REL, sync_plan.new_state.files)

    def test_global_config_round_trip(self) -> None:
        save_global_config(GlobalConfig(inject_myai_rule=False))
        cfg = load_global_config()
        self.assertFalse(cfg.inject_myai_rule)
        self.assertTrue(global_config_path().is_file())

    def test_legacy_registry_inject_myai_rule_fallback(self) -> None:
        from myai.agentsync.registry import load, save

        save({"master": None, "repos": [], "inject_myai_rule": False})
        self.assertFalse(get_inject_myai_rule_default())

    def test_set_clears_legacy_registry_inject_myai_rule(self) -> None:
        from myai.agentsync.registry import load, save

        save({"master": None, "repos": [], "inject_myai_rule": False})
        set_inject_myai_rule_default(True)
        self.assertTrue(get_inject_myai_rule_default())
        self.assertNotIn("inject_myai_rule", load())
        self.assertTrue(global_config_path().is_file())


class TestSyncUntrustedRepo(unittest.TestCase):
    """.myai/state.json and the working tree come with the clone; sync must not
    let them reach outside the repo or outside the dirs sync manages."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self._config_dir = self.tmp / ".myai"
        self._config_dir.mkdir()
        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = str(self.tmp / "state")
        self._patch = patch("myai.paths.global_myai_dir", return_value=self._config_dir)
        self._patch.start()

        master = self.tmp / "master"
        (master / "rules").mkdir(parents=True)
        (master / "rules" / "general.md").write_text("General rule\n", encoding="utf-8")
        set_master(master)

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        save_config(self.repo, RepoConfig(agents=["claude"], rules=["general"]))

        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("keep", encoding="utf-8")

    def tearDown(self) -> None:
        self._patch.stop()
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home
        self._tmp.cleanup()

    def _write_state(self, files: dict, blocks: dict | None = None) -> None:
        state = {"files": files, "blocks": blocks or {}}
        (self.repo / ".myai" / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def test_state_traversal_and_absolute_paths_are_not_deleted(self) -> None:
        self._write_state({"../outside": "x", str(self.outside): "y"})
        result = sync_repo(self.repo)

        self.assertIsNone(result.error)
        self.assertTrue((self.outside / "keep.txt").is_file())
        self.assertEqual(len(result.warnings), 2)
        self.assertFalse([a for a in result.actions if a.kind == "delete"])

    def test_state_cannot_delete_unmanaged_repo_paths(self) -> None:
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("code", encoding="utf-8")
        mine = self.repo / ".claude" / "skills" / "mine"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("mine", encoding="utf-8")
        # in the repo but not ours, and a managed dir itself rather than a file in it
        self._write_state({"src": "x", ".claude/skills": "y"})

        result = sync_repo(self.repo)

        self.assertIsNone(result.error)
        self.assertTrue((self.repo / "src" / "main.py").is_file())
        self.assertTrue((mine / "SKILL.md").is_file())

    def test_junk_state_entries_are_dropped_after_sync(self) -> None:
        self._write_state({"../outside": "x"})
        sync_repo(self.repo)
        self.assertNotIn("../outside", load_state(self.repo).files)
        self.assertEqual(sync_repo(self.repo).warnings, [])

    def test_stale_managed_file_is_still_pruned(self) -> None:
        stale = self.repo / ".claude" / "rules" / "old.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("old", encoding="utf-8")
        self._write_state({".claude/rules/old.md": "x"})

        sync_repo(self.repo)

        self.assertFalse(stale.exists())

    def test_symlink_at_destination_is_replaced_not_written_through(self) -> None:
        victim = self.outside / "rcfile"
        victim.write_text("original", encoding="utf-8")
        dest = self.repo / ".claude" / "rules" / "general.md"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(victim)

        result = sync_repo(self.repo)

        self.assertIsNone(result.error)
        self.assertEqual(victim.read_text(encoding="utf-8"), "original")
        self.assertFalse(dest.is_symlink())
        self.assertIn("General rule", dest.read_text(encoding="utf-8"))

    def test_symlinked_parent_leaving_repo_fails_before_writing(self) -> None:
        (self.repo / ".claude").symlink_to(self.outside)

        result = sync_repo(self.repo)

        self.assertIn("resolves outside the repo", result.error or "")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["keep.txt"])

    def test_block_target_symlink_inside_repo_is_written_through(self) -> None:
        save_config(
            self.repo,
            RepoConfig(agents=["claude"], rules=["general"], nested_rules=False),
        )
        (self.repo / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")

        result = sync_repo(self.repo)

        self.assertIsNone(result.error)
        self.assertTrue((self.repo / "CLAUDE.md").is_symlink())
        text = (self.repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# mine", text)
        self.assertIn("General rule", text)

    def test_block_target_symlink_leaving_repo_is_refused(self) -> None:
        save_config(
            self.repo,
            RepoConfig(agents=["claude"], rules=["general"], nested_rules=False),
        )
        victim = self.outside / "rcfile"
        victim.write_text("original", encoding="utf-8")
        (self.repo / "CLAUDE.md").symlink_to(victim)

        result = sync_repo(self.repo)

        self.assertIn("leaves the repo", result.error or "")
        self.assertEqual(victim.read_text(encoding="utf-8"), "original")

    def test_state_block_entry_outside_known_targets_is_ignored(self) -> None:
        victim = self.outside / "rcfile"
        victim.write_text("a\n<!-- myai:begin -->\nx\n<!-- myai:end -->\n", encoding="utf-8")
        before = victim.read_text(encoding="utf-8")
        self._write_state({}, {"../outside/rcfile": True})

        result = sync_repo(self.repo)

        self.assertIsNone(result.error)
        self.assertEqual(victim.read_text(encoding="utf-8"), before)

    def test_compute_sync_raises_unsafe_path_for_escaping_target(self) -> None:
        (self.repo / ".claude").symlink_to(self.outside)
        with self.assertRaises(UnsafePathError):
            compute_sync(self.repo, load_config(self.repo), RepoState())


if __name__ == "__main__":
    unittest.main()
