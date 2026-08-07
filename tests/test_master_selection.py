import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.agentsync.config import ConfigError, AGENTS, load_config, normalize_agents
from myai.agentsync.global_config import load_global_sync_config
from myai.agentsync.master import (
    MasterError,
    expand_csv_list,
    load_skill,
    load_subagent,
    normalize_name_list,
    resolve_selection,
)
from myai.agentsync.registry import set_master
from myai.cli import main


def _write_master(root: Path) -> Path:
    master = root / "master"
    (master / "rules").mkdir(parents=True)
    (master / "rules" / "general.md").write_text(
        "---\ndescription: General\n---\nBe nice.\n",
        encoding="utf-8",
    )
    (master / "rules" / "langs").mkdir()
    (master / "rules" / "langs" / "python.md").write_text(
        "---\ndescription: Python\n---\nUse uv.\n",
        encoding="utf-8",
    )
    (master / "rules" / "langs" / "rust.md").write_text(
        "---\ndescription: Rust\n---\nUse cargo.\n",
        encoding="utf-8",
    )
    skill = master / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    skill2 = master / "skills" / "other"
    skill2.mkdir(parents=True)
    (skill2 / "SKILL.md").write_text("# Other\n", encoding="utf-8")
    (master / "subagents").mkdir()
    (master / "subagents" / "reviewer.md").write_text(
        "---\nname: reviewer\n---\nReview.\n",
        encoding="utf-8",
    )
    (master / "subagents" / "tester.md").write_text(
        "---\nname: tester\n---\nTest.\n",
        encoding="utf-8",
    )
    return master


class TestExpandCsvList(unittest.TestCase):
    def test_splits_and_dedupes(self) -> None:
        self.assertEqual(
            expand_csv_list(["a", "b,c", " a ", "c,,d"]),
            ["a", "b", "c", "d"],
        )

    def test_empty(self) -> None:
        self.assertEqual(expand_csv_list([]), [])
        self.assertEqual(expand_csv_list([",", "  "]), [])


class TestNormalizeNameList(unittest.TestCase):
    def test_csv(self) -> None:
        self.assertEqual(normalize_name_list(["a,b", "c"]), ["a", "b", "c"])

    def test_all_wins(self) -> None:
        self.assertEqual(normalize_name_list(["a", "all", "b"]), ["all"])
        self.assertEqual(normalize_name_list(["all,foo"]), ["all"])


class TestNormalizeAgents(unittest.TestCase):
    def test_omit_and_all(self) -> None:
        self.assertEqual(normalize_agents(None), list(AGENTS))
        self.assertEqual(normalize_agents([]), list(AGENTS))
        self.assertEqual(normalize_agents(["all"]), list(AGENTS))
        self.assertEqual(normalize_agents(["claude,all"]), list(AGENTS))

    def test_csv(self) -> None:
        self.assertEqual(normalize_agents(["claude,pi"]), ["claude", "pi"])
        self.assertEqual(normalize_agents(["claude", "pi"]), ["claude", "pi"])

    def test_unknown(self) -> None:
        with self.assertRaises(ConfigError):
            normalize_agents(["nope"])


class TestResolveSelection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.master = _write_master(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dir_expand(self) -> None:
        rules, skills, subs = resolve_selection(self.master, ["langs"], [], [])
        self.assertEqual(sorted(r.name for r in rules), ["langs/python", "langs/rust"])
        self.assertEqual(skills, [])
        self.assertEqual(subs, [])

    def test_csv_with_dir(self) -> None:
        rules, _, _ = resolve_selection(self.master, ["langs,general"], [], [])
        names = sorted(r.name for r in rules)
        self.assertEqual(names, ["general", "langs/python", "langs/rust"])

    def test_repeated_and_csv_dedupe(self) -> None:
        rules, _, _ = resolve_selection(
            self.master, ["general", "langs/python", "general,langs"], [], []
        )
        names = [r.name for r in rules]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("general", names)
        self.assertIn("langs/python", names)
        self.assertIn("langs/rust", names)

    def test_all_rules(self) -> None:
        rules, _, _ = resolve_selection(self.master, ["all"], [], [])
        names = sorted(r.name for r in rules)
        self.assertEqual(names, ["general", "langs/python", "langs/rust"])

    def test_all_mixed_wins(self) -> None:
        rules, skills, subs = resolve_selection(
            self.master, ["general", "all"], ["demo", "all"], ["reviewer,all"]
        )
        self.assertEqual(sorted(r.name for r in rules), ["general", "langs/python", "langs/rust"])
        self.assertEqual(sorted(s.name for s in skills), ["demo", "other"])
        self.assertEqual(sorted(s.name for s in subs), ["reviewer", "tester"])

    def test_all_skills_and_subagents(self) -> None:
        _, skills, subs = resolve_selection(self.master, [], ["all"], ["all"])
        self.assertEqual(sorted(s.name for s in skills), ["demo", "other"])
        self.assertEqual(sorted(s.name for s in subs), ["reviewer", "tester"])

    def test_skill_name_traversal_rejected(self) -> None:
        # A traversal name would otherwise render to home/skills/../../x and
        # escape the agent home entirely. Real target, so "not found" can't be
        # what saves us here.
        outside = self.master.parent / "outside-skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text("# pwned\n", encoding="utf-8")
        for bad in ["../../outside-skill", "../other", "/abs/skill", "sub/dir", "..", ""]:
            with self.assertRaises(MasterError):
                load_skill(self.master, bad)

    def test_subagent_name_traversal_rejected(self) -> None:
        for bad in ["../rules/general", "../../outside", "/abs/sub", "sub/dir", ".."]:
            with self.assertRaises(MasterError):
                load_subagent(self.master, bad)

    def test_valid_names_still_load(self) -> None:
        self.assertEqual(load_skill(self.master, "demo").name, "demo")
        self.assertEqual(load_subagent(self.master, "reviewer").name, "reviewer")

    def test_traversal_via_selection_rejected(self) -> None:
        with self.assertRaises(MasterError):
            resolve_selection(self.master, [], ["../../outside"], [])
        with self.assertRaises(MasterError):
            resolve_selection(self.master, [], [], ["../rules/general"])

    def test_empty_means_nothing(self) -> None:
        rules, skills, subs = resolve_selection(self.master, [], [], [])
        self.assertEqual(rules, [])
        self.assertEqual(skills, [])
        self.assertEqual(subs, [])


class TestCliSelectionFlags(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.master = _write_master(root)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.myai_dir = root / "myai-home"
        self.myai_dir.mkdir()
        self.state_dir = root / "state"
        self.state_dir.mkdir()

        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = str(self.state_dir)
        self._patches = [
            patch("myai.paths.global_myai_dir", return_value=self.myai_dir),
        ]
        for p in self._patches:
            p.start()
        set_master(self.master)

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_init_csv_and_all(self) -> None:
        code = main([
            "init",
            "--path", str(self.repo),
            "--agent", "claude,pi",
            "--rule", "langs,general",
            "--skill", "all",
            "-y",
        ])
        self.assertEqual(code, 0)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.agents, ["claude", "pi"])
        self.assertEqual(cfg.rules, ["langs", "general"])
        self.assertEqual(cfg.skills, ["all"])

    def test_global_init_csv(self) -> None:
        code = main([
            "global", "init",
            "--agent", "claude,cursor",
            "--rule", "all",
            "--skill", "demo,other",
            "-y",
        ])
        self.assertEqual(code, 0)
        cfg = load_global_sync_config()
        self.assertEqual(cfg.agents, ["claude", "cursor"])
        self.assertEqual(cfg.rules, ["all"])
        self.assertEqual(cfg.skills, ["demo", "other"])
        data = json.loads((self.myai_dir / "global.json").read_text(encoding="utf-8"))
        self.assertEqual(data["rules"], ["all"])


if __name__ == "__main__":
    unittest.main()
