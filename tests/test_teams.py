import argparse
import os
import sqlite3
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from myai.commands import teams as teams_cmd
from myai.paths import (
    teams_daemon_lock_path,
    teams_db_path,
    teams_prompts_dir,
    teams_transcripts_dir,
    teams_worktrees_dir,
)
from myai.teams import db as db_mod
from myai.teams.config import (
    ConfigError,
    apply_concurrency_defaults,
    default_config,
    install_default_prompts,
)
from myai.teams.db import connect, ensure_state_dirs, migrate, open_db
from myai.teams.editor import EditorError, EditRejected, edit_text
from myai.teams.ids import IdError, format_epic_id, parse_epic_id, parse_task_id
from myai.teams.store import (
    abandon_epic,
    approve_epic,
    create_epic,
    create_project,
    create_task,
    get_project,
    get_task,
    list_projects,
    parse_task_edit_document,
    project_config,
    status_overview,
    task_edit_document,
    update_task_from_edit,
)


class TeamsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"MYAI_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()


class TestPathsAndMigrate(TeamsTestCase):
    def test_open_db_creates_layout_and_schema(self) -> None:
        conn = open_db()
        try:
            self.assertTrue(teams_db_path().is_file())
            self.assertTrue(teams_transcripts_dir().is_dir())
            self.assertTrue(teams_worktrees_dir().is_dir())
            self.assertTrue(teams_daemon_lock_path().is_file())
            self.assertTrue(teams_prompts_dir().is_dir())
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for name in (
                "projects",
                "epics",
                "tasks",
                "runs",
                "events",
                "approvals",
                "messages",
                "schema_migrations",
            ):
                self.assertIn(name, tables)
            version = conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchone()[0]
            self.assertEqual(version, 1)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            conn.close()

    def test_open_approval_unique_indexes(self) -> None:
        conn = open_db()
        try:
            proj = create_project(
                conn, name="p", workspace_path=str(self.home / "ws")
            )
            epic = create_epic(
                conn,
                project_id=proj["id"],
                title="e",
                goal="g",
            )
            task = create_task(
                conn,
                project_id=proj["id"],
                title="t",
                body="",
            )
            conn.execute(
                """
                INSERT INTO approvals(
                  task_id, epic_id, project_id, kind, summary, requested_at
                ) VALUES (?, NULL, ?, 'stage_gate', 'a', 't0')
                """,
                (task["id"], proj["id"]),
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO approvals(
                      task_id, epic_id, project_id, kind, summary, requested_at
                    ) VALUES (?, NULL, ?, 'stage_gate', 'b', 't1')
                    """,
                    (task["id"], proj["id"]),
                )
                conn.commit()
            conn.rollback()
            conn.execute(
                """
                INSERT INTO approvals(
                  task_id, epic_id, project_id, kind, summary, requested_at
                ) VALUES (NULL, ?, ?, 'epic_approval', 'a', 't0')
                """,
                (epic["id"], proj["id"]),
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO approvals(
                      task_id, epic_id, project_id, kind, summary, requested_at
                    ) VALUES (NULL, ?, ?, 'epic_approval', 'b', 't1')
                    """,
                    (epic["id"], proj["id"]),
                )
                conn.commit()
        finally:
            conn.close()


class TestConfig(TeamsTestCase):
    def test_concurrency_defaults(self) -> None:
        cfg = {
            "roster": {
                "developer": {
                    "backend": "cursor",
                    "prompt": "/tmp/dev.md",
                }
            },
            "pipeline": [
                {"stage": "develop", "role": "developer", "gate": "auto"}
            ],
        }
        out = apply_concurrency_defaults(cfg)
        self.assertEqual(out["concurrency"]["per_stage"]["develop"], 1)
        self.assertEqual(out["concurrency"]["pm"], 1)
        self.assertNotIn("max_total", out["concurrency"])

    def test_default_config_roster_points_at_xdg_prompts(self) -> None:
        install_default_prompts()
        cfg = default_config()
        for role in ("pm", "designer", "developer", "qa"):
            prompt = Path(cfg["roster"][role]["prompt"])
            self.assertTrue(str(prompt).startswith(str(teams_prompts_dir())))
            self.assertTrue(prompt.is_file())
        self.assertTrue(Path(cfg["roster"]["pm"]["groom_prompt"]).is_file())


class TestStoreAndCli(TeamsTestCase):
    def test_init_and_project_list(self) -> None:
        ws = self.home / "repo"
        ws.mkdir()
        ns = argparse.Namespace(name="demo", path=str(ws))
        self.assertEqual(teams_cmd.run_init(ns), 0)
        self.assertEqual(
            teams_cmd.run_project_list(argparse.Namespace()), 0
        )
        conn = open_db()
        try:
            rows = list_projects(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "demo")
        finally:
            conn.close()
        self.assertEqual(teams_cmd.run_init(ns), 1)

    def test_project_edit_round_trip(self) -> None:
        conn = open_db()
        try:
            install_default_prompts()
            proj = create_project(
                conn, name="demo", workspace_path=str(self.home)
            )
            cfg = default_config()
            cfg["concurrency"]["per_stage"]["develop"] = 7
            yaml_text = yaml.safe_dump(cfg, sort_keys=False)

            def fake_edit(initial, *, suffix=".yaml", parse=lambda t: t):
                return parse(yaml_text)

            with patch("myai.commands.teams.edit_text", side_effect=fake_edit):
                code = teams_cmd.run_project_edit(
                    argparse.Namespace(name="demo")
                )
            self.assertEqual(code, 0)
            got = project_config(get_project(conn, project_id=proj["id"]))
            self.assertEqual(got["concurrency"]["per_stage"]["develop"], 7)
        finally:
            conn.close()

    def test_task_standalone_and_epic_linked(self) -> None:
        conn = open_db()
        try:
            install_default_prompts()
            proj = create_project(
                conn, name="demo", workspace_path=str(self.home)
            )
            standalone = create_task(
                conn,
                project_id=proj["id"],
                title="chore",
                body="do it",
            )
            self.assertIsNone(standalone["epic_id"])
            self.assertEqual(standalone["status"], "backlog")

            epic = create_epic(
                conn,
                project_id=proj["id"],
                title="feat",
                goal="ship it",
                status="awaiting_approval",
            )
            draft = create_task(
                conn,
                project_id=proj["id"],
                title="impl",
                body="code",
                epic_id=epic["id"],
                status="draft",
            )
            self.assertEqual(draft["status"], "draft")

            approve_epic(conn, epic["id"])
            draft2 = get_task(conn, draft["id"])
            self.assertEqual(draft2["status"], "backlog")
            epic2 = conn.execute(
                "SELECT * FROM epics WHERE id = ?", (epic["id"],)
            ).fetchone()
            self.assertEqual(epic2["status"], "executing")
            self.assertEqual(epic2["branch"], f"teams/E-{epic['id']}")

            conn.execute(
                "UPDATE epics SET status = 'awaiting_review' WHERE id = ?",
                (epic["id"],),
            )
            conn.commit()
            approve_epic(conn, epic["id"])
            epic3 = conn.execute(
                "SELECT status FROM epics WHERE id = ?", (epic["id"],)
            ).fetchone()
            self.assertEqual(epic3["status"], "done")
        finally:
            conn.close()

    def test_epic_abandon(self) -> None:
        conn = open_db()
        try:
            install_default_prompts()
            proj = create_project(
                conn, name="demo", workspace_path=str(self.home)
            )
            epic = create_epic(
                conn, project_id=proj["id"], title="x", goal="y"
            )
            abandon_epic(conn, epic["id"])
            row = conn.execute(
                "SELECT status FROM epics WHERE id = ?", (epic["id"],)
            ).fetchone()
            self.assertEqual(row["status"], "abandoned")
        finally:
            conn.close()

    def test_task_edit_bumps_version(self) -> None:
        conn = open_db()
        try:
            install_default_prompts()
            proj = create_project(
                conn, name="demo", workspace_path=str(self.home)
            )
            task = create_task(
                conn,
                project_id=proj["id"],
                title="t",
                body="old",
            )
            doc = task_edit_document(task)
            fields = parse_task_edit_document(doc)
            fields["title"] = "t2"
            fields["body"] = "new"
            fields["priority"] = 5
            updated = update_task_from_edit(conn, task["id"], **fields)
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["title"], "t2")
            self.assertEqual(updated["body"], "new")
            self.assertEqual(updated["priority"], 5)
        finally:
            conn.close()

    def test_status_counts(self) -> None:
        conn = open_db()
        try:
            install_default_prompts()
            proj = create_project(
                conn, name="demo", workspace_path=str(self.home)
            )
            create_task(
                conn,
                project_id=proj["id"],
                title="a",
                status="running",
                stage="develop",
            )
            create_task(
                conn,
                project_id=proj["id"],
                title="b",
                status="ready",
                stage="design",
            )
            create_task(
                conn,
                project_id=proj["id"],
                title="c",
                status="ready",
                stage="design",
            )
            overview = status_overview(conn, proj["id"])
            self.assertEqual(overview["task_counts"]["running"], 1)
            self.assertEqual(overview["task_counts"]["ready"], 2)
            self.assertEqual(len(overview["in_flight"]), 1)
            self.assertEqual(overview["queued_by_stage"]["design"], 2)
        finally:
            conn.close()

    def test_cli_status_without_daemon(self) -> None:
        ws = self.home / "repo"
        ws.mkdir()
        self.assertEqual(
            teams_cmd.run_init(
                argparse.Namespace(name="demo", path=str(ws))
            ),
            0,
        )
        code = teams_cmd.run_status(argparse.Namespace(project=None))
        self.assertEqual(code, 0)

    def test_ids(self) -> None:
        self.assertEqual(parse_epic_id("E-12"), 12)
        self.assertEqual(parse_task_id("t-3"), 3)
        self.assertEqual(format_epic_id(12), "E-12")
        with self.assertRaises(IdError):
            parse_epic_id("12")


class TestMigrationAtomicity(TeamsTestCase):
    def test_interrupted_migration_replays(self) -> None:
        """A crash between schema apply and version stamp must not brick the DB."""
        ensure_state_dirs()
        conn = connect()
        try:
            sql = (
                resources.files("myai.teams.migrations") / "001_initial.sql"
            ).read_text(encoding="utf-8")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY)"
            )
            conn.commit()
            # Same script migrate() runs, but killed before the version stamp.
            with self.assertRaises(sqlite3.Error):
                conn.executescript(f"BEGIN;\n{sql}\nROLLBACK;\nSELECT bad_fn();")
            conn.rollback()
        finally:
            conn.close()

        conn = open_db()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("projects", tables)
            versions = [
                row[0]
                for row in conn.execute("SELECT version FROM schema_migrations")
            ]
            self.assertEqual(versions, [1])
        finally:
            conn.close()

    def test_failed_migration_leaves_no_partial_schema(self) -> None:
        ensure_state_dirs()
        conn = connect()
        try:
            with patch.object(
                db_mod, "_MIGRATIONS", [(1, "001_initial.sql"), (2, "nope.sql")]
            ):
                with self.assertRaises(Exception):
                    migrate(conn)
            self.assertFalse(conn.in_transaction)
            versions = [
                row[0]
                for row in conn.execute("SELECT version FROM schema_migrations")
            ]
            self.assertEqual(versions, [1])
        finally:
            conn.close()


class TestEditor(TeamsTestCase):
    def _fake_editor(self, writes: str) -> Path:
        """An editor that writes `writes` to whichever arg is the file path."""
        script = self.home / "fake-editor"
        script.write_text(
            f'#!/bin/sh\nfor f in "$@"; do :; done\nprintf {writes!r} > "$f"\n'
        )
        script.chmod(0o755)
        return script

    def test_editor_with_flags_is_split(self) -> None:
        """$EDITOR routinely carries flags; it must not be exec'd as argv[0]."""
        script = self._fake_editor("edited")
        with patch.dict(os.environ, {"EDITOR": f"{script} --wait"}):
            os.environ.pop("VISUAL", None)
            self.assertEqual(edit_text("seed"), "edited")

    def test_unrunnable_editor_raises_editor_error(self) -> None:
        with patch.dict(os.environ, {"EDITOR": "no-such-editor-xyz"}):
            os.environ.pop("VISUAL", None)
            with self.assertRaises(EditorError):
                edit_text("seed")

    def test_rejected_edit_is_kept_on_disk(self) -> None:
        script = self._fake_editor("whatever")

        def boom(text: str) -> str:
            raise ConfigError("nope")

        with patch.dict(os.environ, {"EDITOR": str(script)}):
            os.environ.pop("VISUAL", None)
            with self.assertRaises(EditRejected) as ctx:
                edit_text("seed", parse=boom)
        kept = ctx.exception.path
        self.assertTrue(kept.is_file())
        self.assertEqual(kept.read_text(), "whatever")
        self.assertIn("nope", str(ctx.exception))
        self.assertIn(str(kept), str(ctx.exception))
        kept.unlink()

    def test_accepted_edit_is_cleaned_up(self) -> None:
        script = self._fake_editor("ok")
        made: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            made.append(path)
            return fd, path

        with patch.dict(os.environ, {"EDITOR": str(script)}):
            os.environ.pop("VISUAL", None)
            with patch("myai.teams.editor.tempfile.mkstemp", side_effect=spy):
                self.assertEqual(edit_text("seed"), "ok")
        self.assertEqual(len(made), 1)
        self.assertFalse(Path(made[0]).exists())

    def test_bad_blocked_by_is_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            parse_task_edit_document(
                "---\ntitle: t\nblocked_by: [not-a-number]\n---\n\nbody"
            )

    def test_cli_task_edit_reports_rejected_edit(self) -> None:
        conn = open_db()
        try:
            install_default_prompts()
            proj = create_project(
                conn, name="demo", workspace_path=str(self.home)
            )
            task = create_task(conn, project_id=proj["id"], title="t", body="keep")
        finally:
            conn.close()

        def bad_edit(initial, *, suffix=".md", parse=lambda t: t):
            return parse("---\ntitle: t\nblocked_by: [nope]\n---\n\nnew spec")

        with patch("myai.commands.teams.edit_text", side_effect=bad_edit):
            code = teams_cmd.run_task_edit(
                argparse.Namespace(task_id=f"T-{task['id']}")
            )
        self.assertEqual(code, 1)
        conn = open_db()
        try:
            unchanged = get_task(conn, task["id"])
            self.assertEqual(unchanged["body"], "keep")
            self.assertEqual(unchanged["version"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
