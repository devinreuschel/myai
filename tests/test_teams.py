import argparse
import io
import os
import shlex
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.commands import teams as teams_cmd
from myai.paths import (
    teams_artifacts_dir,
    teams_bot_home,
    teams_bots_dir,
    teams_daemon_lock_path,
    teams_db_path,
    teams_root,
)
from myai.teams import db as db_mod
from myai.teams.config import (
    ConfigError,
    bot_config_from_yaml,
    default_bot_config,
    ensure_bot_home,
    load_bot_config,
    validate_bot_config,
)
from myai.teams.db import connect, ensure_state_dirs, migrate, open_db
from myai.teams.editor import EditorError, EditRejected, edit_text
from myai.teams.ids import (
    IdError,
    format_task_id,
    parse_principal_id,
    parse_task_id,
    slugify,
)
from myai.teams.store import (
    MAILBOX_PRIORITY,
    StoreError,
    create_bot,
    create_task,
    create_user,
    get_dm,
    get_principal,
    get_task,
    parse_task_edit_document,
    pending_mailbox,
    post_message,
    status_overview,
    task_edit_document,
    task_messages,
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

    def _seed(self, conn: sqlite3.Connection, *bots: str) -> None:
        create_user(conn, user_id="sam", name="Sam")
        for bot_id in bots:
            create_bot(conn, bot_id=bot_id, name=bot_id.title())


class TestPathsAndMigrate(TeamsTestCase):
    def test_open_db_creates_layout_and_schema(self) -> None:
        conn = open_db()
        try:
            self.assertEqual(teams_db_path(), teams_root() / "teams.db")
            self.assertTrue(teams_db_path().is_file())
            self.assertTrue(teams_bots_dir().is_dir())
            self.assertTrue(teams_artifacts_dir().is_dir())
            self.assertTrue(teams_daemon_lock_path().is_file())
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for name in (
                "principals",
                "contacts",
                "conversations",
                "participants",
                "messages",
                "message_recipients",
                "tasks",
                "mailbox",
                "sessions",
                "turns",
                "events",
                "schema_migrations",
            ):
                self.assertIn(name, tables)
            versions = [
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            self.assertEqual(versions, [1])
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            conn.close()

    def test_pre_pivot_db_is_moved_aside_not_migrated(self) -> None:
        ensure_state_dirs()
        old = sqlite3.connect(str(teams_db_path()))
        old.executescript(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);"
            "INSERT INTO schema_migrations VALUES (1), (2);"
            "CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);"
            "INSERT INTO projects(name) VALUES ('demo');"
        )
        old.close()

        conn = open_db()
        try:
            self._seed(conn)
        finally:
            conn.close()

        kept = list(teams_root().glob("teams.db.pre-pivot-*"))
        self.assertEqual(len(kept), 1)
        survivor = sqlite3.connect(str(kept[0]))
        try:
            self.assertEqual(
                survivor.execute("SELECT name FROM projects").fetchone()[0], "demo"
            )
        finally:
            survivor.close()

        # a current DB is left alone on the next open
        open_db().close()
        self.assertEqual(len(list(teams_root().glob("teams.db.pre-pivot-*"))), 1)

    def test_turns_and_events_cannot_be_rewritten(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev")
            conn.execute(
                "INSERT INTO sessions(bot_id, route, daemon_epoch, started_at) "
                "VALUES ('dev', 'brain', 'e1', 't0')"
            )
            conn.execute(
                "INSERT INTO turns(session_id, seq, role, content_json, origin, event_time) "
                "VALUES (1, 1, 'user', '{}', 'first_hand', 't0')"
            )
            conn.commit()
            for statement in (
                "UPDATE turns SET origin = 'recalled'",
                "DELETE FROM turns",
                "UPDATE events SET kind = 'x'",
                "DELETE FROM events",
            ):
                with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                    conn.execute(statement)
                conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO turns(session_id, seq, role, content_json, origin, "
                    "event_time) VALUES (1, 2, 'user', '{}', 'guessed', 't0')"
                )
        finally:
            conn.close()


class TestBotConfig(TeamsTestCase):
    def test_defaults_fill_in(self) -> None:
        cfg = validate_bot_config({"name": " Dev Lead ", "budgets": {"wake_turns": 3}})
        self.assertEqual(cfg["name"], "Dev Lead")
        self.assertEqual(cfg["preset"], "hands-off")
        self.assertEqual(cfg["budgets"], {"wake_turns": 3, "wake_seconds": 60})

    def test_rejects_bad_configs(self) -> None:
        base = default_bot_config("Dev")
        for bad in (
            [],
            {**base, "name": ""},
            {**base, "preset": "yolo"},
            {**base, "budget": {}},
            {**base, "budgets": {"wake_turns": 0}},
            {**base, "budgets": {"wake_turns": True}},
            {**base, "budgets": {"wake_minutes": 5}},
            {**base, "job": 7},
        ):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                validate_bot_config(bad)
        with self.assertRaises(ConfigError):
            bot_config_from_yaml("name: [unclosed")

    def test_bot_home_is_seeded_once(self) -> None:
        home = ensure_bot_home("dev", default_bot_config("Dev", "ships slices"))
        self.assertEqual(home, teams_bot_home("dev"))
        for sub in ("playbooks", "workspace"):
            self.assertTrue((home / sub).is_dir())
        self.assertIn("ships slices", (home / "persona.md").read_text())
        self.assertEqual(load_bot_config("dev")["job"], "ships slices")

        (home / "persona.md").write_text("mine", encoding="utf-8")
        ensure_bot_home("dev", default_bot_config("Other"))
        self.assertEqual((home / "persona.md").read_text(), "mine")
        self.assertEqual(load_bot_config("dev")["name"], "Dev")


class TestStore(TeamsTestCase):
    def test_single_user(self) -> None:
        conn = open_db()
        try:
            self._seed(conn)
            with self.assertRaises(StoreError) as ctx:
                create_user(conn, user_id="alex", name="Alex")
            self.assertIn("already initialized", str(ctx.exception))
        finally:
            conn.close()

    def test_bot_needs_a_user(self) -> None:
        conn = open_db()
        try:
            with self.assertRaises(StoreError) as ctx:
                create_bot(conn, bot_id="dev", name="Dev")
            self.assertIn("teams init", str(ctx.exception))
        finally:
            conn.close()

    def test_create_bot_opens_dm_and_contact(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev")
            dm = get_dm(conn, "sam", "dev")
            self.assertEqual(dm["kind"], "dm")
            contacts = conn.execute(
                "SELECT contact_id FROM contacts WHERE bot_id = 'dev'"
            ).fetchall()
            self.assertEqual([r[0] for r in contacts], ["sam"])

            # ids are one space: a bot cannot take the user's id, or another bot's
            for taken in ("sam", "dev"):
                with self.assertRaises(StoreError) as ctx:
                    create_bot(conn, bot_id=taken, name="x")
                self.assertIn("already taken", str(ctx.exception))
            self.assertEqual(get_principal(conn, "sam")["kind"], "user")
            self.assertFalse(conn.in_transaction)
        finally:
            conn.close()

    def test_addressing_wakes_only_bots_addressed(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev")
            dm = get_dm(conn, "sam", "dev")["id"]
            post_message(
                conn, conversation_id=dm, sender_id="sam", body="fyi", recipients=[]
            )
            self.assertEqual(pending_mailbox(conn, "dev"), [])

            msg = post_message(
                conn, conversation_id=dm, sender_id="sam", body="go", recipients=["dev"]
            )
            (item,) = pending_mailbox(conn, "dev")
            self.assertEqual(item["kind"], "user_message")
            self.assertEqual(item["ref_id"], msg["id"])
            self.assertEqual(item["priority"], MAILBOX_PRIORITY["user_message"])

            # a reply addressed to the human enqueues nothing: only bots have mailboxes
            post_message(
                conn, conversation_id=dm, sender_id="dev", body="ok", recipients=["sam"]
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_mailbox_orders_user_before_bots(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev", "qa")
            now = "2026-01-01T00:00:00+00:00"
            conn.execute(
                "INSERT INTO conversations(kind, created_at) VALUES ('group', ?)", (now,)
            )
            group = conn.execute("SELECT MAX(id) FROM conversations").fetchone()[0]
            conn.executemany(
                "INSERT INTO participants VALUES (?, ?, ?)",
                [(group, p, now) for p in ("sam", "dev", "qa")],
            )
            conn.execute("INSERT INTO contacts VALUES ('qa', 'dev')")
            conn.commit()

            post_message(
                conn, conversation_id=group, sender_id="qa", body="pr open",
                recipients=["dev"],
            )
            post_message(
                conn, conversation_id=group, sender_id="sam", body="status?",
                recipients=["dev"],
            )
            kinds = [row["kind"] for row in pending_mailbox(conn, "dev")]
            self.assertEqual(kinds, ["user_message", "bot_message"])
            # membership is visibility, not waking
            self.assertEqual(pending_mailbox(conn, "qa"), [])
        finally:
            conn.close()

    def test_address_book_is_enforced(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev", "qa")
            now = "2026-01-01T00:00:00+00:00"
            conn.execute(
                "INSERT INTO conversations(kind, created_at) VALUES ('group', ?)", (now,)
            )
            group = conn.execute("SELECT MAX(id) FROM conversations").fetchone()[0]
            conn.executemany(
                "INSERT INTO participants VALUES (?, ?, ?)",
                [(group, p, now) for p in ("sam", "dev", "qa")],
            )
            conn.commit()

            with self.assertRaises(StoreError) as ctx:
                post_message(
                    conn, conversation_id=group, sender_id="dev", body="hi",
                    recipients=["qa"],
                )
            self.assertIn("no contact entry", str(ctx.exception))

            dm = get_dm(conn, "sam", "dev")["id"]
            for kwargs, needle in (
                ({"sender_id": "qa", "recipients": []}, "is not in conversation"),
                ({"sender_id": "sam", "recipients": ["qa"]}, "recipients not in"),
                ({"sender_id": "sam", "recipients": ["sam"]}, "its sender"),
            ):
                with self.assertRaises(StoreError) as ctx:
                    post_message(conn, conversation_id=dm, body="x", **kwargs)
                self.assertIn(needle, str(ctx.exception))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0
            )
            self.assertFalse(conn.in_transaction)
        finally:
            conn.close()

    def test_task_is_a_thread_with_an_owner(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev")
            dm = get_dm(conn, "sam", "dev")["id"]
            task = create_task(
                conn, conversation_id=dm, title="add oauth", created_by="sam",
                owner_id="dev", body="token refresh first",
            )
            self.assertEqual(task["status"], "open")
            self.assertEqual(task["owner_id"], "dev")
            (opening,) = task_messages(conn, task["id"])
            self.assertEqual(opening["body"], "token refresh first")
            (item,) = pending_mailbox(conn, "dev")
            self.assertEqual(item["ref_id"], opening["id"])

            untitled_body = create_task(
                conn, conversation_id=dm, title="chore", created_by="sam", owner_id="dev"
            )
            self.assertEqual(task_messages(conn, untitled_body["id"])[0]["body"], "chore")

            with self.assertRaises(StoreError):
                create_task(
                    conn, conversation_id=dm, title="x", created_by="sam",
                    owner_id="nobody",
                )
            kinds = [
                r[0] for r in conn.execute("SELECT kind FROM events ORDER BY id")
            ]
            self.assertEqual(kinds.count("task_created"), 2)
            self.assertEqual(kinds.count("message_posted"), 2)
        finally:
            conn.close()

    def test_task_edit_bumps_version_and_detects_concurrent_change(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev")
            dm = get_dm(conn, "sam", "dev")["id"]
            task = create_task(
                conn, conversation_id=dm, title="t", created_by="sam", owner_id="dev"
            )
            stale_doc = task_edit_document(task)

            fields = parse_task_edit_document(stale_doc)
            fields.update(title="t2", status="active", handoff="next: tests")
            updated = update_task_from_edit(conn, task["id"], **fields)
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["title"], "t2")
            self.assertEqual(updated["status"], "active")
            self.assertEqual(updated["handoff"], "next: tests")
            self.assertEqual(
                parse_task_edit_document(task_edit_document(updated))["handoff"],
                "next: tests",
            )

            # an edit that began on version 1 must not clobber version 2
            with self.assertRaises(StoreError) as ctx:
                update_task_from_edit(
                    conn, task["id"], **parse_task_edit_document(stale_doc)
                )
            self.assertIn("changed while it was being edited", str(ctx.exception))

            fields = parse_task_edit_document(task_edit_document(updated))
            fields["owner_id"] = "stranger"
            with self.assertRaises(StoreError):
                update_task_from_edit(conn, task["id"], **fields)
        finally:
            conn.close()

    def test_status_counts(self) -> None:
        conn = open_db()
        try:
            self._seed(conn, "dev")
            dm = get_dm(conn, "sam", "dev")["id"]
            for title in ("a", "b", "c"):
                create_task(
                    conn, conversation_id=dm, title=title, created_by="sam",
                    owner_id="dev",
                )
            done = parse_task_edit_document(task_edit_document(get_task(conn, 1)))
            done["status"] = "done"
            update_task_from_edit(conn, 1, **done)

            overview = status_overview(conn)
            self.assertEqual(overview["task_counts"], {"done": 1, "open": 2})
            (bot,) = overview["bots"]
            self.assertEqual(bot["open_tasks"], 2)
            self.assertEqual(bot["pending"], 3)
        finally:
            conn.close()

    def test_ids(self) -> None:
        self.assertEqual(parse_task_id("t-3"), 3)
        self.assertEqual(format_task_id(12), "T-12")
        self.assertEqual(slugify("Dev Lead!"), "dev-lead")
        self.assertEqual(parse_principal_id(" qa-2 "), "qa-2")
        for bad in ("12", "T12"):
            with self.assertRaises(IdError):
                parse_task_id(bad)
        for bad in ("Dev", "-dev", "a/b", "", "x" * 33):
            with self.assertRaises(IdError, msg=bad):
                parse_principal_id(bad)
        with self.assertRaises(IdError):
            slugify("!!!")


class TestCli(TeamsTestCase):
    def _run(self, fn, **kwargs) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(argparse.Namespace(**kwargs))
        return code, out.getvalue(), err.getvalue()

    def _init(self) -> None:
        self.assertEqual(self._run(teams_cmd.run_init, user="Sam")[0], 0)
        code, out, _ = self._run(
            teams_cmd.run_bot_new, name="Dev Lead", id=None, job="ships slices"
        )
        self.assertEqual(code, 0)
        self.assertIn("dev-lead", out)

    def test_init_once(self) -> None:
        code, out, _ = self._run(teams_cmd.run_init, user="Sam")
        self.assertEqual(code, 0)
        self.assertIn("sam", out)
        code, _, err = self._run(teams_cmd.run_init, user="Sam")
        self.assertEqual(code, 1)
        self.assertIn("already initialized", err)

    def test_bot_new_list_and_duplicate(self) -> None:
        self._init()
        self.assertTrue((teams_bot_home("dev-lead") / "bot.yaml").is_file())
        code, out, _ = self._run(teams_cmd.run_bot_list)
        self.assertEqual(code, 0)
        self.assertIn("dev-lead\tDev Lead\tships slices", out)

        code, _, err = self._run(
            teams_cmd.run_bot_new, name="Dev Lead", id=None, job=""
        )
        self.assertEqual(code, 1)
        self.assertIn("already taken", err)
        code, _, err = self._run(teams_cmd.run_bot_new, name="x", id="Bad Id", job="")
        self.assertEqual(code, 1)

    def test_bot_edit_keeps_the_users_yaml_and_syncs_the_name(self) -> None:
        self._init()
        edited = "# my notes\nname: Lead\njob: ships slices\npreset: supervised\n"

        def fake_edit(initial, *, suffix=".yaml", parse=lambda t: t):
            return parse(edited)

        with patch("myai.commands.teams.edit_text", side_effect=fake_edit):
            code, _, _ = self._run(teams_cmd.run_bot_edit, bot=None)
        self.assertEqual(code, 0)
        path = teams_bot_home("dev-lead") / "bot.yaml"
        self.assertEqual(path.read_text(), edited)
        self.assertEqual(load_bot_config("dev-lead")["preset"], "supervised")
        conn = open_db()
        try:
            self.assertEqual(get_principal(conn, "dev-lead")["name"], "Lead")
        finally:
            conn.close()

    def test_bot_edit_rejects_bad_config_and_leaves_file(self) -> None:
        self._init()
        path = teams_bot_home("dev-lead") / "bot.yaml"
        before = path.read_text()

        def bad_edit(initial, *, suffix=".yaml", parse=lambda t: t):
            return parse("name: Lead\npreset: yolo\n")

        with patch("myai.commands.teams.edit_text", side_effect=bad_edit):
            code, _, err = self._run(teams_cmd.run_bot_edit, bot="dev-lead")
        self.assertEqual(code, 1)
        self.assertIn("preset", err)
        self.assertEqual(path.read_text(), before)

    def test_task_add_show_list(self) -> None:
        self._init()
        code, out, _ = self._run(
            teams_cmd.run_task_add, title="chore", body="do it", bot=None
        )
        self.assertEqual(code, 0)
        self.assertIn("T-1\topen\tdev-lead\tchore", out)

        code, out, _ = self._run(teams_cmd.run_task_list, bot=None, status=None)
        self.assertEqual(code, 0)
        self.assertIn("T-1", out)
        code, out, _ = self._run(teams_cmd.run_task_list, bot="dev-lead", status="done")
        self.assertIn("no tasks", out)

        code, out, _ = self._run(teams_cmd.run_task_show, task_id="T-1")
        self.assertEqual(code, 0)
        self.assertIn("id:       T-1", out)
        self.assertIn("owner:    dev-lead", out)
        self.assertIn("sam: do it", out)

        code, _, err = self._run(teams_cmd.run_task_show, task_id="T-9")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_task_add_needs_a_bot(self) -> None:
        self.assertEqual(self._run(teams_cmd.run_init, user="Sam")[0], 0)
        code, _, err = self._run(teams_cmd.run_task_add, title="t", body="", bot=None)
        self.assertEqual(code, 1)
        self.assertIn("no bots", err)

    def test_status(self) -> None:
        code, _, err = self._run(teams_cmd.run_status)
        self.assertEqual(code, 1)
        self.assertIn("teams init", err)

        self._init()
        self._run(teams_cmd.run_task_add, title="chore", body="", bot=None)
        code, out, _ = self._run(teams_cmd.run_status)
        self.assertEqual(code, 0)
        self.assertIn("user: sam", out)
        self.assertIn("dev-lead\tqueued wakes: 1\topen tasks: 1", out)
        self.assertIn("open: 1", out)

    def test_pre_pivot_db_is_reported(self) -> None:
        ensure_state_dirs()
        old = sqlite3.connect(str(teams_db_path()))
        old.executescript("CREATE TABLE projects (id INTEGER PRIMARY KEY);")
        old.close()
        code, _, err = self._run(teams_cmd.run_init, user="Sam")
        self.assertEqual(code, 0)
        self.assertIn("pre-pivot", err)


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
            self.assertIn("principals", tables)
            versions = [
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
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
        payload = self.home / "fake-editor-payload"
        payload.write_text(writes, encoding="utf-8")
        script = self.home / "fake-editor"
        script.write_text(
            f'#!/bin/sh\nfor f in "$@"; do :; done\ncat {shlex.quote(str(payload))} > "$f"\n'
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

    def test_bad_task_frontmatter_is_config_error(self) -> None:
        for doc in (
            "no frontmatter",
            "---\ntitle: t\n",
            "---\n- a list\n---\n",
            "---\nstatus: open\n---\n",
            "---\ntitle: t\nstatus: running\n---\n",
            "---\ntitle: t\nowner: 7\n---\n",
        ):
            with self.assertRaises(ConfigError, msg=doc):
                parse_task_edit_document(doc)

    def test_cli_task_edit_keeps_a_rejected_edit(self) -> None:
        """The update runs inside parse, so a store rejection keeps the file too."""
        conn = open_db()
        try:
            self._seed(conn, "dev")
            task = create_task(
                conn, conversation_id=get_dm(conn, "sam", "dev")["id"], title="t",
                created_by="sam", owner_id="dev",
            )
        finally:
            conn.close()

        script = self._fake_editor("---\ntitle: t\nowner: stranger\nversion: 1\n---\n")
        err = io.StringIO()
        with patch.dict(os.environ, {"EDITOR": str(script)}):
            os.environ.pop("VISUAL", None)
            with redirect_stderr(err):
                code = teams_cmd.run_task_edit(
                    argparse.Namespace(task_id=f"T-{task['id']}")
                )
        self.assertEqual(code, 1)
        self.assertIn("not in the task's conversation", err.getvalue())
        self.assertIn("edit kept at", err.getvalue())
        kept = Path(err.getvalue().split("edit kept at ")[1].strip())
        self.assertTrue(kept.is_file())
        kept.unlink()

        conn = open_db()
        try:
            unchanged = get_task(conn, task["id"])
            self.assertEqual(unchanged["owner_id"], "dev")
            self.assertEqual(unchanged["version"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
