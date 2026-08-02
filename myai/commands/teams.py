from __future__ import annotations

import argparse
import sys
from pathlib import Path

from myai.teams.config import (
    ConfigError,
    config_from_yaml,
    config_to_yaml,
    default_config,
    install_default_prompts,
)
from myai.teams.db import open_db
from myai.teams.editor import EditorError, edit_text
from myai.teams.ids import (
    IdError,
    format_epic_id,
    format_task_id,
    parse_epic_id,
    parse_task_id,
)
from myai.teams.store import (
    StoreError,
    abandon_epic,
    approve_epic,
    create_epic,
    create_project,
    create_task,
    get_epic,
    get_task,
    list_epics,
    list_projects,
    list_tasks,
    parse_task_edit_document,
    project_config,
    resolve_project,
    status_overview,
    task_edit_document,
    update_project_config,
    update_task_from_edit,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "teams",
        help="Agentic teams board (SQLite; no daemon required for view/edit)",
    )
    teams_sub = parser.add_subparsers(dest="teams_command", required=True)
    _register_init(teams_sub)
    _register_project(teams_sub)
    _register_epic(teams_sub)
    _register_task(teams_sub)
    _register_status(teams_sub)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return args.func(args)


def _register_init(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "init",
        help="Create teams DB and register the first project",
    )
    p.add_argument("--name", help="Project name (default: directory name)")
    p.add_argument(
        "--path",
        default=".",
        help="Workspace path (default: cwd)",
    )
    p.set_defaults(func=run_init)


def _register_project(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("project", help="Manage projects")
    sub = p.add_subparsers(dest="project_command", required=True)

    new_p = sub.add_parser("new", help="Register a new project")
    new_p.add_argument("--name", help="Project name (default: directory name)")
    new_p.add_argument("--path", default=".", help="Workspace path")
    new_p.set_defaults(func=run_project_new)

    edit_p = sub.add_parser("edit", help="Edit project config in $EDITOR (YAML)")
    edit_p.add_argument(
        "name",
        nargs="?",
        help="Project name (default: sole project)",
    )
    edit_p.set_defaults(func=run_project_edit)

    list_p = sub.add_parser("list", help="List projects")
    list_p.set_defaults(func=run_project_list)

    p.set_defaults(func=run)


def _register_epic(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("epic", help="Manage epics")
    sub = p.add_subparsers(dest="epic_command", required=True)

    add_p = sub.add_parser("add", help="Create an epic")
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--goal", required=True)
    add_p.add_argument(
        "--status",
        default="grooming",
        help="Initial status (default: grooming)",
    )
    add_p.add_argument("--base-branch", default="main")
    add_p.add_argument("--project", help="Project name")
    add_p.set_defaults(func=run_epic_add)

    list_p = sub.add_parser("list", help="List epics")
    list_p.add_argument("--project", help="Project name")
    list_p.set_defaults(func=run_epic_list)

    show_p = sub.add_parser("show", help="Show an epic")
    show_p.add_argument("epic_id", help="E-<id>")
    show_p.set_defaults(func=run_epic_show)

    approve_p = sub.add_parser(
        "approve",
        help="Approve scope (awaiting_approval→executing) or review (→done)",
    )
    approve_p.add_argument("epic_id", help="E-<id>")
    approve_p.set_defaults(func=run_epic_approve)

    abandon_p = sub.add_parser("abandon", help="Abandon an epic")
    abandon_p.add_argument("epic_id", help="E-<id>")
    abandon_p.set_defaults(func=run_epic_abandon)

    p.set_defaults(func=run)


def _register_task(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("task", help="Manage tasks")
    sub = p.add_subparsers(dest="task_command", required=True)

    add_p = sub.add_parser("add", help="Create a task")
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--body", default="")
    add_p.add_argument("--epic", help="E-<id> (omit for standalone)")
    add_p.add_argument(
        "--status",
        default=None,
        help="Status (default: draft if --epic else backlog)",
    )
    add_p.add_argument("--priority", type=int, default=0)
    add_p.add_argument("--stage")
    add_p.add_argument("--role")
    add_p.add_argument("--project", help="Project name")
    add_p.set_defaults(func=run_task_add)

    edit_p = sub.add_parser("edit", help="Edit task in $EDITOR (YAML+markdown)")
    edit_p.add_argument("task_id", help="T-<id>")
    edit_p.set_defaults(func=run_task_edit)

    list_p = sub.add_parser("list", help="List tasks")
    list_p.add_argument("--project", help="Project name")
    list_p.add_argument("--epic", help="Filter by E-<id>")
    list_p.set_defaults(func=run_task_list)

    show_p = sub.add_parser("show", help="Show a task")
    show_p.add_argument("task_id", help="T-<id>")
    show_p.set_defaults(func=run_task_show)

    p.set_defaults(func=run)


def _register_status(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "status",
        help="Board overview: counts, in-flight, queued-by-stage",
    )
    p.add_argument(
        "project",
        nargs="?",
        help="Project name (default: sole project)",
    )
    p.set_defaults(func=run_status)


def run_init(args: argparse.Namespace) -> int:
    try:
        workspace = Path(args.path).resolve()
        name = args.name or workspace.name
        conn = open_db()
        try:
            install_default_prompts()
            if list_projects(conn):
                print(
                    "teams DB already has projects; "
                    "use `myai teams project new` to add another",
                    file=sys.stderr,
                )
                return 1
            proj = create_project(
                conn,
                name=name,
                workspace_path=str(workspace),
                config=default_config(),
            )
        finally:
            conn.close()
    except (StoreError, ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"initialized teams DB; project {proj['name']} ({workspace})")
    return 0


def run_project_new(args: argparse.Namespace) -> int:
    try:
        workspace = Path(args.path).resolve()
        name = args.name or workspace.name
        conn = open_db()
        try:
            install_default_prompts()
            proj = create_project(
                conn,
                name=name,
                workspace_path=str(workspace),
                config=default_config(),
            )
        finally:
            conn.close()
    except (StoreError, ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created project {proj['name']} ({workspace})")
    return 0


def run_project_edit(args: argparse.Namespace) -> int:
    try:
        conn = open_db()
        try:
            proj = resolve_project(conn, args.name)
            cfg = project_config(proj)
            new_cfg = edit_text(
                config_to_yaml(cfg), suffix=".yaml", parse=config_from_yaml
            )
            update_project_config(conn, proj["id"], new_cfg)
        finally:
            conn.close()
    except EditorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (StoreError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"updated project {proj['name']} config")
    return 0


def run_project_list(args: argparse.Namespace) -> int:
    try:
        conn = open_db()
        try:
            rows = list_projects(conn)
        finally:
            conn.close()
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no projects")
        return 0
    for row in rows:
        print(f"{row['id']}\t{row['name']}\t{row['workspace_path']}")
    return 0


def run_epic_add(args: argparse.Namespace) -> int:
    try:
        conn = open_db()
        try:
            proj = resolve_project(conn, args.project)
            epic = create_epic(
                conn,
                project_id=proj["id"],
                title=args.title,
                goal=args.goal,
                base_branch=args.base_branch,
                status=args.status,
            )
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{format_epic_id(epic['id'])}\t{epic['status']}\t{epic['title']}")
    return 0


def run_epic_list(args: argparse.Namespace) -> int:
    try:
        conn = open_db()
        try:
            proj = resolve_project(conn, args.project)
            rows = list_epics(conn, proj["id"])
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no epics")
        return 0
    for row in rows:
        print(
            f"{format_epic_id(row['id'])}\t{row['status']}\t"
            f"{row['title']}\tproject={row['project_id']}"
        )
    return 0


def run_epic_show(args: argparse.Namespace) -> int:
    try:
        epic_id = parse_epic_id(args.epic_id)
        conn = open_db()
        try:
            epic = get_epic(conn, epic_id)
            tasks = list_tasks(conn, epic_id=epic_id)
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"id:      {format_epic_id(epic['id'])}")
    print(f"title:   {epic['title']}")
    print(f"status:  {epic['status']}")
    print(f"branch:  {epic['branch'] or '-'}")
    print(f"base:    {epic['base_branch']}")
    print(f"version: {epic['version']}")
    print(f"goal:\n{epic['goal']}")
    if tasks:
        print("tasks:")
        for t in tasks:
            print(
                f"  {format_task_id(t['id'])}\t{t['status']}\t{t['title']}"
            )
    return 0


def run_epic_approve(args: argparse.Namespace) -> int:
    try:
        epic_id = parse_epic_id(args.epic_id)
        conn = open_db()
        try:
            epic = approve_epic(conn, epic_id)
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{format_epic_id(epic['id'])} approved → {epic['status']}")
    return 0


def run_epic_abandon(args: argparse.Namespace) -> int:
    try:
        epic_id = parse_epic_id(args.epic_id)
        conn = open_db()
        try:
            epic = abandon_epic(conn, epic_id)
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{format_epic_id(epic['id'])} abandoned")
    return 0


def run_task_add(args: argparse.Namespace) -> int:
    try:
        conn = open_db()
        try:
            proj = resolve_project(conn, args.project)
            epic_id = parse_epic_id(args.epic) if args.epic else None
            if args.status:
                status = args.status
            else:
                status = "draft" if epic_id is not None else "backlog"
            task = create_task(
                conn,
                project_id=proj["id"],
                title=args.title,
                body=args.body,
                epic_id=epic_id,
                status=status,
                stage=args.stage,
                role=args.role,
                priority=args.priority,
            )
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    epic_s = (
        format_epic_id(task["epic_id"]) if task["epic_id"] else "standalone"
    )
    print(
        f"{format_task_id(task['id'])}\t{task['status']}\t"
        f"{task['title']}\t{epic_s}"
    )
    return 0


def run_task_edit(args: argparse.Namespace) -> int:
    try:
        task_id = parse_task_id(args.task_id)
        conn = open_db()
        try:
            task = get_task(conn, task_id)
            fields = edit_text(
                task_edit_document(task),
                suffix=".md",
                parse=parse_task_edit_document,
            )
            task = update_task_from_edit(conn, task_id, **fields)
        finally:
            conn.close()
    except EditorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (StoreError, IdError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{format_task_id(task['id'])} updated (version {task['version']})"
    )
    return 0


def run_task_list(args: argparse.Namespace) -> int:
    try:
        conn = open_db()
        try:
            proj = resolve_project(conn, args.project)
            epic_id = parse_epic_id(args.epic) if args.epic else None
            rows = list_tasks(
                conn, project_id=proj["id"], epic_id=epic_id
            )
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no tasks")
        return 0
    for row in rows:
        epic_s = format_epic_id(row["epic_id"]) if row["epic_id"] else "-"
        stage = row["stage"] or "-"
        print(
            f"{format_task_id(row['id'])}\t{row['status']}\t"
            f"p={row['priority']}\t{stage}\t{epic_s}\t{row['title']}"
        )
    return 0


def run_task_show(args: argparse.Namespace) -> int:
    try:
        task_id = parse_task_id(args.task_id)
        conn = open_db()
        try:
            task = get_task(conn, task_id)
        finally:
            conn.close()
    except (StoreError, IdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    blocked = task["blocked_by"] or "[]"
    print(f"id:         {format_task_id(task['id'])}")
    print(f"title:      {task['title']}")
    print(f"status:     {task['status']}")
    print(f"priority:   {task['priority']}")
    print(f"stage:      {task['stage'] or '-'}")
    print(f"role:       {task['role'] or '-'}")
    print(
        f"epic:       "
        f"{format_epic_id(task['epic_id']) if task['epic_id'] else '-'}"
    )
    print(f"version:    {task['version']}")
    print(f"loop_count: {task['loop_count']}")
    print(f"blocked_by: {blocked}")
    print(f"body:\n{task['body']}")
    return 0


def run_status(args: argparse.Namespace) -> int:
    try:
        conn = open_db()
        try:
            proj = resolve_project(conn, args.project)
            overview = status_overview(conn, proj["id"])
        finally:
            conn.close()
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"project: {proj['name']}")
    print("epics:")
    if overview["epic_counts"]:
        for status, n in sorted(overview["epic_counts"].items()):
            print(f"  {status}: {n}")
    else:
        print("  (none)")
    print("tasks:")
    if overview["task_counts"]:
        for status, n in sorted(overview["task_counts"].items()):
            print(f"  {status}: {n}")
    else:
        print("  (none)")
    print("in-flight:")
    if overview["in_flight"]:
        for t in overview["in_flight"]:
            stage = t["stage"] or "-"
            print(
                f"  {format_task_id(t['id'])}\t{stage}\t{t['title']}"
            )
    else:
        print("  (none)")
    print("queued-by-stage (ready):")
    if overview["queued_by_stage"]:
        for stage, n in overview["queued_by_stage"].items():
            print(f"  {stage}: {n}")
    else:
        print("  (none)")
    return 0
