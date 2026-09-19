from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from myai.paths import teams_bot_home
from myai.teams.config import (
    BOT_CONFIG_FILE,
    TASK_STATUSES,
    ConfigError,
    bot_config_from_yaml,
    default_bot_config,
    ensure_bot_home,
    load_bot_config,
)
from myai.teams.db import move_aside_pre_pivot, open_db
from myai.teams.editor import EditorError, edit_text
from myai.teams.ids import (
    IdError,
    format_task_id,
    parse_principal_id,
    parse_task_id,
    slugify,
)
from myai.teams.store import (
    StoreError,
    create_bot,
    create_task,
    create_user,
    get_dm,
    get_task,
    get_user,
    list_bots,
    list_tasks,
    parse_task_edit_document,
    rename_principal,
    resolve_bot,
    status_overview,
    task_edit_document,
    task_messages,
    update_task_from_edit,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "teams",
        help="Agentic teams: long-lived bots (state only; nothing runs without the daemon)",
    )
    teams_sub = parser.add_subparsers(dest="teams_command", required=True)
    _register_init(teams_sub)
    _register_bot(teams_sub)
    _register_task(teams_sub)
    _register_status(teams_sub)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return args.func(args)


def _register_init(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("init", help="Create the teams DB and register you")
    p.add_argument("--user", help="Your display name (default: login name)")
    p.set_defaults(func=run_init)


def _register_bot(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("bot", help="Manage bots")
    sub = p.add_subparsers(dest="bot_command", required=True)

    new_p = sub.add_parser("new", help="Register a bot and create its home")
    new_p.add_argument("name", help="Display name")
    new_p.add_argument("--id", help="Bot id (default: derived from the name)")
    new_p.add_argument("--job", default="", help="One line on what this bot is for")
    new_p.set_defaults(func=run_bot_new)

    edit_p = sub.add_parser("edit", help=f"Edit a bot's {BOT_CONFIG_FILE} in $EDITOR")
    edit_p.add_argument("bot", nargs="?", help="Bot id (default: sole bot)")
    edit_p.set_defaults(func=run_bot_edit)

    list_p = sub.add_parser("list", help="List bots")
    list_p.set_defaults(func=run_bot_list)

    p.set_defaults(func=run)


def _register_task(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("task", help="Manage tasks")
    sub = p.add_subparsers(dest="task_command", required=True)

    add_p = sub.add_parser("add", help="Open a task thread in your DM with a bot")
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--body", default="", help="Opening message (default: title)")
    add_p.add_argument("--bot", help="Owner bot id (default: sole bot)")
    add_p.set_defaults(func=run_task_add)

    edit_p = sub.add_parser("edit", help="Edit task in $EDITOR (YAML+markdown)")
    edit_p.add_argument("task_id", help="T-<id>")
    edit_p.set_defaults(func=run_task_edit)

    list_p = sub.add_parser("list", help="List tasks")
    list_p.add_argument("--bot", help="Filter by owner bot id")
    list_p.add_argument("--status", choices=TASK_STATUSES)
    list_p.set_defaults(func=run_task_list)

    show_p = sub.add_parser("show", help="Show a task and its thread")
    show_p.add_argument("task_id", help="T-<id>")
    show_p.set_defaults(func=run_task_show)

    p.set_defaults(func=run)


def _register_status(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "status", help="Overview: bots, queued wakes, tasks by status"
    )
    p.set_defaults(func=run_status)


def _open() -> sqlite3.Connection:
    moved = move_aside_pre_pivot()
    if moved is not None:
        print(f"note: moved the pre-pivot teams DB aside to {moved}", file=sys.stderr)
    return open_db()


def run_init(args: argparse.Namespace) -> int:
    try:
        name = args.user or getpass.getuser()
        conn = _open()
        try:
            user = create_user(conn, user_id=slugify(name), name=name)
        finally:
            conn.close()
    except (StoreError, IdError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"initialized teams DB; user {user['id']}")
    return 0


def run_bot_new(args: argparse.Namespace) -> int:
    try:
        bot_id = parse_principal_id(args.id) if args.id else slugify(args.name)
        config = default_bot_config(args.name, args.job)
        conn = _open()
        try:
            bot = create_bot(conn, bot_id=bot_id, name=config["name"])
        finally:
            conn.close()
        home = ensure_bot_home(bot_id, config)
    except (StoreError, IdError, ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created bot {bot['id']} ({home})")
    return 0


def run_bot_edit(args: argparse.Namespace) -> int:
    try:
        conn = _open()
        try:
            bot = resolve_bot(conn, args.bot)
            ensure_bot_home(bot["id"], default_bot_config(bot["name"]))
            path = teams_bot_home(bot["id"]) / BOT_CONFIG_FILE
            # written back verbatim: the file is the user's, comments included
            edited, config = edit_text(
                path.read_text(encoding="utf-8"),
                suffix=".yaml",
                parse=lambda t: (t, bot_config_from_yaml(t)),
            )
            path.write_text(edited, encoding="utf-8")
            if config["name"] != bot["name"]:
                rename_principal(conn, bot["id"], config["name"])
        finally:
            conn.close()
    except (EditorError, StoreError, ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"updated bot {bot['id']}")
    return 0


def run_bot_list(args: argparse.Namespace) -> int:
    try:
        conn = _open()
        try:
            rows = list_bots(conn)
        finally:
            conn.close()
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no bots")
        return 0
    for row in rows:
        try:
            job = load_bot_config(row["id"])["job"] or "-"
        except ConfigError:
            job = f"(unreadable {BOT_CONFIG_FILE})"
        print(f"{row['id']}\t{row['name']}\t{job}")
    return 0


def run_task_add(args: argparse.Namespace) -> int:
    try:
        conn = _open()
        try:
            user = get_user(conn)
            bot = resolve_bot(conn, args.bot)
            task = create_task(
                conn,
                conversation_id=get_dm(conn, user["id"], bot["id"])["id"],
                title=args.title,
                created_by=user["id"],
                owner_id=bot["id"],
                body=args.body,
            )
        finally:
            conn.close()
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{format_task_id(task['id'])}\t{task['status']}\t"
        f"{task['owner_id']}\t{task['title']}"
    )
    return 0


def run_task_edit(args: argparse.Namespace) -> int:
    try:
        task_id = parse_task_id(args.task_id)
        conn = _open()
        try:
            # apply inside parse so a rejected update keeps the edited file
            task = edit_text(
                task_edit_document(get_task(conn, task_id)),
                suffix=".md",
                parse=lambda text: update_task_from_edit(
                    conn, task_id, **parse_task_edit_document(text)
                ),
            )
        finally:
            conn.close()
    except (EditorError, StoreError, IdError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{format_task_id(task['id'])} updated (version {task['version']})")
    return 0


def run_task_list(args: argparse.Namespace) -> int:
    try:
        conn = _open()
        try:
            owner = resolve_bot(conn, args.bot)["id"] if args.bot else None
            rows = list_tasks(conn, owner_id=owner, status=args.status)
        finally:
            conn.close()
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no tasks")
        return 0
    for row in rows:
        print(
            f"{format_task_id(row['id'])}\t{row['status']}\t"
            f"{row['owner_id'] or '-'}\t{row['title']}"
        )
    return 0


def run_task_show(args: argparse.Namespace) -> int:
    try:
        task_id = parse_task_id(args.task_id)
        conn = _open()
        try:
            task = get_task(conn, task_id)
            messages = task_messages(conn, task_id)
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"id:       {format_task_id(task['id'])}")
    print(f"title:    {task['title']}")
    print(f"status:   {task['status']}")
    print(f"owner:    {task['owner_id'] or '-'}")
    print(f"version:  {task['version']}")
    print(f"handoff:\n{task['handoff'] or '(none)'}")
    print("thread:")
    for msg in messages:
        print(f"  [{msg['created_at']}] {msg['sender_id']}: {msg['body']}")
    return 0


def run_status(args: argparse.Namespace) -> int:
    try:
        conn = _open()
        try:
            user = get_user(conn)
            overview = status_overview(conn)
        finally:
            conn.close()
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"user: {user['id']}")
    print("bots:")
    if overview["bots"]:
        for bot in overview["bots"]:
            print(
                f"  {bot['id']}\tqueued wakes: {bot['pending']}\t"
                f"open tasks: {bot['open_tasks']}"
            )
    else:
        print("  (none)")
    print("tasks:")
    if overview["task_counts"]:
        for status, n in sorted(overview["task_counts"].items()):
            print(f"  {status}: {n}")
    else:
        print("  (none)")
    return 0
