import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.agentsync.global_config import (
    GlobalSyncConfig,
    GlobalSyncState,
    load_global_sync_config,
    load_global_sync_state,
    save_global_sync_config,
)
from myai.agentsync.global_homes import (
    CURSOR_RULES_WARNING,
    GLOBAL_MYAI_MANAGED_RULE,
    agent_home,
    build_global_plan,
)
from myai.agentsync.global_sync import (
    apply_global_sync,
    compute_global_sync,
    sync_global,
)
from myai.agentsync.master import Frontmatter, Rule, Skill, Subagent
from myai.agentsync.registry import set_master
from myai.cli import main


def _rule(name: str, body: str = "rule body", **frontmatter: object) -> Rule:
    fm = Frontmatter(raw=dict(frontmatter))
    return Rule(name=name, path=Path(f"/master/rules/{name}.md"), frontmatter=fm, body=body)


def _skill(name: str, path: Path) -> Skill:
    return Skill(name=name, path=path)


class TestAgentHome(unittest.TestCase):
    def test_default_homes(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("myai.agentsync.global_homes.Path.home", return_value=home):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                    os.environ.pop("PI_CODING_AGENT_DIR", None)
                    self.assertEqual(agent_home("claude"), home / ".claude")
                    self.assertEqual(agent_home("cursor"), home / ".cursor")
                    self.assertEqual(agent_home("pi"), home / ".pi" / "agent")

    def test_env_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            claude = Path(tmp) / "claude-cfg"
            pi = Path(tmp) / "pi-agent"
            claude.mkdir()
            pi.mkdir()
            with patch.dict(
                os.environ,
                {"CLAUDE_CONFIG_DIR": str(claude), "PI_CODING_AGENT_DIR": str(pi)},
            ):
                self.assertEqual(agent_home("claude"), claude.resolve())
                self.assertEqual(agent_home("pi"), pi.resolve())


class TestBuildGlobalPlan(unittest.TestCase):
    def test_claude_nested_rules_and_skills(self) -> None:
        with TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / "demo"
            skill_path.mkdir()
            (skill_path / "SKILL.md").write_text("# demo\n", encoding="utf-8")
            plan = build_global_plan(
                ["claude"],
                [_rule("general")],
                [_skill("demo", skill_path)],
                [],
                nested_rules=True,
            )
            self.assertIn("claude:rules/general.md", plan.files)
            self.assertIn("claude:skills/demo", plan.files)
            self.assertNotIn("CLAUDE.md", [k.split(":", 1)[1] for k in plan.blocks])

    def test_claude_flat_rules(self) -> None:
        plan = build_global_plan(
            ["claude"],
            [_rule("general")],
            [],
            [],
            nested_rules=False,
        )
        self.assertIn("claude:CLAUDE.md", plan.blocks)
        self.assertIn("## general", plan.blocks["claude:CLAUDE.md"])
        self.assertIn("Global rules (myai)", plan.blocks["claude:CLAUDE.md"])

    def test_pi_flattens_and_append_system(self) -> None:
        plan = build_global_plan(
            ["pi"],
            [_rule("general")],
            [],
            [],
            inject_myai_rule=True,
        )
        self.assertIn("pi:AGENTS.md", plan.blocks)
        self.assertIn("pi:APPEND_SYSTEM.md", plan.files)
        self.assertEqual(
            plan.files["pi:APPEND_SYSTEM.md"].content,
            GLOBAL_MYAI_MANAGED_RULE,
        )

    def test_cursor_skills_only_warns_on_rules(self) -> None:
        with TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / "demo"
            skill_path.mkdir()
            (skill_path / "SKILL.md").write_text("# demo\n", encoding="utf-8")
            plan = build_global_plan(
                ["cursor"],
                [_rule("general")],
                [_skill("demo", skill_path)],
                [],
            )
            self.assertIn(CURSOR_RULES_WARNING, plan.warnings)
            self.assertIn("cursor:skills/demo", plan.files)
            self.assertFalse(any(k.startswith("cursor:rules") for k in plan.files))
            self.assertFalse(any(k.startswith("cursor:") for k in plan.blocks))

    def test_claude_subagents(self) -> None:
        sub = Subagent(
            name="reviewer",
            path=Path("/master/subagents/reviewer.md"),
            frontmatter=Frontmatter(raw={"description": "reviews"}),
            body="review things",
        )
        plan = build_global_plan(["claude"], [], [], [sub])
        self.assertIn("claude:agents/reviewer.md", plan.files)

    def test_cursor_no_guardrail(self) -> None:
        plan = build_global_plan(
            ["cursor"],
            [],
            [],
            [],
            inject_myai_rule=True,
        )
        self.assertFalse(plan.files)
        self.assertFalse(plan.blocks)


class TestGlobalSyncIntegration(unittest.TestCase):
    def setUp(self) -> None:
        ws_tmp = Path(__file__).resolve().parents[1] / ".tmp-tests"
        ws_tmp.mkdir(exist_ok=True)
        self._tmp = TemporaryDirectory(dir=ws_tmp)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.myai_dir = self.root / ".myai"
        self.myai_dir.mkdir()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.master = self.root / "master"
        self.master.mkdir()
        (self.master / "rules").mkdir()
        (self.master / "rules" / "general.md").write_text(
            "---\ndescription: General\n---\nBe nice.\n",
            encoding="utf-8",
        )
        skill = self.master / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
        (self.master / "subagents").mkdir()
        (self.master / "subagents" / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: reviews\n---\nReview code.\n",
            encoding="utf-8",
        )

        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = str(self.state_dir)
        self._patches = [
            patch("myai.agentsync.global_homes.agent_home", side_effect=self._agent_home),
            patch("myai.agentsync.global_sync.agent_home", side_effect=self._agent_home),
            patch("myai.commands.global_cmd.agent_home", side_effect=self._agent_home),
            patch("myai.paths.global_myai_dir", return_value=self.myai_dir),
        ]
        for p in self._patches:
            p.start()
        set_master(self.master)

    def _agent_home(self, agent: str) -> Path:
        # Use non-dot names so sandbox/CI can write under the temp tree.
        if agent == "claude":
            return self.home / "claude"
        if agent == "cursor":
            return self.home / "cursor"
        if agent == "pi":
            return self.home / "pi-agent"
        raise ValueError(agent)

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_sync_writes_homes_and_prunes_skill(self) -> None:
        save_global_sync_config(
            GlobalSyncConfig(
                agents=["claude", "cursor", "pi"],
                rules=["general"],
                skills=["demo"],
                subagents=["reviewer"],
                inject_myai_rule=True,
            )
        )
        plan = compute_global_sync(load_global_sync_config(), GlobalSyncState())
        self.assertTrue(any(a.path.startswith("cursor:skills") for a in plan.actions))
        self.assertTrue(plan.warnings)
        apply_global_sync(plan, GlobalSyncState())

        self.assertTrue((self.home / "claude" / "skills" / "demo" / "SKILL.md").is_file())
        self.assertTrue((self.home / "claude" / "rules" / "general.md").is_file())
        self.assertTrue((self.home / "claude" / "agents" / "reviewer.md").is_file())
        self.assertTrue((self.home / "claude" / "rules" / "myai-managed.md").is_file())
        self.assertTrue((self.home / "cursor" / "skills" / "demo" / "SKILL.md").is_file())
        self.assertFalse((self.home / "cursor" / "rules").exists())
        self.assertTrue((self.home / "pi-agent" / "skills" / "demo" / "SKILL.md").is_file())
        self.assertTrue((self.home / "pi-agent" / "AGENTS.md").is_file())
        self.assertTrue((self.home / "pi-agent" / "APPEND_SYSTEM.md").is_file())

        save_global_sync_config(
            GlobalSyncConfig(
                agents=["claude", "cursor", "pi"],
                rules=["general"],
                skills=[],
                subagents=["reviewer"],
                inject_myai_rule=True,
            )
        )
        old = load_global_sync_state()
        plan2 = compute_global_sync(load_global_sync_config(), old)
        self.assertTrue(any(a.kind == "delete" and "skills/demo" in a.path for a in plan2.actions))
        apply_global_sync(plan2, old)
        self.assertFalse((self.home / "claude" / "skills" / "demo").exists())
        self.assertFalse((self.home / "cursor" / "skills" / "demo").exists())
        self.assertFalse((self.home / "pi-agent" / "skills" / "demo").exists())

    def test_config_round_trip(self) -> None:
        cfg = GlobalSyncConfig(
            agents=["claude"],
            skills=["demo"],
            nested_rules=False,
            inject_myai_rule=False,
        )
        save_global_sync_config(cfg)
        loaded = load_global_sync_config()
        self.assertEqual(loaded.agents, ["claude"])
        self.assertEqual(loaded.skills, ["demo"])
        self.assertFalse(loaded.nested_rules)
        self.assertFalse(loaded.inject_myai_rule)

    def test_cli_global_init_and_status(self) -> None:
        code = main([
            "global", "init",
            "--agent", "claude",
            "--skill", "demo",
            "-y",
        ])
        self.assertEqual(code, 0)
        self.assertTrue((self.myai_dir / "global.json").is_file())
        code = main(["global", "status"])
        self.assertEqual(code, 0)
        code = main(["global", "sync", "--dry-run"])
        self.assertEqual(code, 0)

    def test_conflict_on_untracked_rule_file(self) -> None:
        save_global_sync_config(
            GlobalSyncConfig(
                agents=["claude"],
                rules=["general"],
                inject_myai_rule=False,
            )
        )
        target = self.home / "claude" / "rules" / "general.md"
        target.parent.mkdir(parents=True)
        target.write_text("user's local rule\n", encoding="utf-8")

        plan = compute_global_sync(load_global_sync_config(), GlobalSyncState())
        self.assertIn("claude:rules/general.md", plan.conflicts)

        blocked = sync_global(dry_run=False, allow_clobber=False)
        self.assertIsNotNone(blocked.error)
        self.assertIn("claude:rules/general.md", blocked.conflicts)
        self.assertEqual(target.read_text(encoding="utf-8"), "user's local rule\n")

        ok = sync_global(dry_run=False, allow_clobber=True)
        self.assertIsNone(ok.error)
        self.assertIn("Be nice.", target.read_text(encoding="utf-8"))

    def test_conflict_on_untracked_skill_dir(self) -> None:
        save_global_sync_config(
            GlobalSyncConfig(
                agents=["claude"],
                skills=["demo"],
                inject_myai_rule=False,
            )
        )
        skill_dir = self.home / "claude" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# mine\n", encoding="utf-8")
        (skill_dir / "notes.txt").write_text("keep me\n", encoding="utf-8")

        plan = compute_global_sync(load_global_sync_config(), GlobalSyncState())
        self.assertIn("claude:skills/demo", plan.conflicts)

        blocked = sync_global(allow_clobber=False)
        self.assertIsNotNone(blocked.error)
        self.assertTrue((skill_dir / "notes.txt").is_file())

        ok = sync_global(allow_clobber=True)
        self.assertIsNone(ok.error)
        self.assertTrue((skill_dir / "SKILL.md").is_file())
        self.assertFalse((skill_dir / "notes.txt").exists())

    def test_identical_untracked_file_is_not_conflict(self) -> None:
        save_global_sync_config(
            GlobalSyncConfig(
                agents=["claude"],
                rules=["general"],
                inject_myai_rule=False,
            )
        )
        plan = compute_global_sync(load_global_sync_config(), GlobalSyncState())
        content = plan.files["claude:rules/general.md"].content or ""
        target = self.home / "claude" / "rules" / "general.md"
        target.parent.mkdir(parents=True)
        target.write_text(content, encoding="utf-8")

        plan2 = compute_global_sync(load_global_sync_config(), GlobalSyncState())
        self.assertNotIn("claude:rules/general.md", plan2.conflicts)

        result = sync_global(allow_clobber=False)
        self.assertIsNone(result.error)

    def test_tracked_files_are_not_conflicts(self) -> None:
        save_global_sync_config(
            GlobalSyncConfig(
                agents=["claude"],
                rules=["general"],
                inject_myai_rule=False,
            )
        )
        plan = compute_global_sync(load_global_sync_config(), GlobalSyncState())
        apply_global_sync(plan, GlobalSyncState())

        # Edit managed file; still tracked so not a clobber conflict.
        target = self.home / "claude" / "rules" / "general.md"
        target.write_text("locally edited managed file\n", encoding="utf-8")
        plan2 = compute_global_sync(load_global_sync_config(), load_global_sync_state())
        self.assertEqual(plan2.conflicts, [])
        result = sync_global(allow_clobber=False)
        self.assertIsNone(result.error)
        self.assertIn("Be nice.", target.read_text(encoding="utf-8"))

    def test_cli_sync_prompts_abort_on_conflict(self) -> None:
        save_global_sync_config(
            GlobalSyncConfig(
                agents=["claude"],
                rules=["general"],
                inject_myai_rule=False,
            )
        )
        target = self.home / "claude" / "rules" / "general.md"
        target.parent.mkdir(parents=True)
        target.write_text("do not clobber\n", encoding="utf-8")

        with patch("builtins.input", return_value="n"):
            code = main(["global", "sync"])
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "do not clobber\n")

        code = main(["global", "sync", "-y"])
        self.assertEqual(code, 0)
        self.assertIn("Be nice.", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
