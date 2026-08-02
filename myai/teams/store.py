from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import yaml

from myai.teams.config import (
    EPIC_STATUSES,
    TASK_STATUSES,
    ConfigError,
    config_from_json,
    config_to_json,
    default_config,
    validate_config,
)
from myai.teams.db import TeamsDBError
from myai.teams.ids import format_epic_id, format_task_id


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StoreError(TeamsDBError):
    pass


# --- projects ---


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM projects ORDER BY id")
    )


def get_project(
    conn: sqlite3.Connection, *, project_id: int | None = None, name: str | None = None
) -> sqlite3.Row:
    if project_id is not None:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    elif name is not None:
        row = conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()
    else:
        raise StoreError("project id or name required")
    if row is None:
        raise StoreError("project not found")
    return row


def resolve_project(
    conn: sqlite3.Connection, name_or_none: str | None
) -> sqlite3.Row:
    if name_or_none:
        return get_project(conn, name=name_or_none)
    rows = list_projects(conn)
    if not rows:
        raise StoreError("no projects; run myai teams init")
    if len(rows) > 1:
        raise StoreError(
            "multiple projects; pass PROJECT name"
        )
    return rows[0]


def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    workspace_path: str,
    config: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT id FROM projects WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        raise StoreError(f"project {name!r} already exists")
    cfg = validate_config(config if config is not None else default_config())
    cur = conn.execute(
        "INSERT INTO projects(name, workspace_path, config_json) VALUES (?, ?, ?)",
        (name, workspace_path, config_to_json(cfg)),
    )
    conn.commit()
    return get_project(conn, project_id=cur.lastrowid)


def update_project_config(
    conn: sqlite3.Connection, project_id: int, config: dict[str, Any]
) -> sqlite3.Row:
    cfg = validate_config(config)
    conn.execute(
        "UPDATE projects SET config_json = ? WHERE id = ?",
        (config_to_json(cfg), project_id),
    )
    conn.commit()
    return get_project(conn, project_id=project_id)


def project_config(row: sqlite3.Row) -> dict[str, Any]:
    return config_from_json(row["config_json"])


# --- epics ---


def create_epic(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    title: str,
    goal: str,
    base_branch: str = "main",
    status: str = "grooming",
) -> sqlite3.Row:
    if status not in EPIC_STATUSES:
        raise StoreError(f"invalid epic status: {status}")
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO epics(
          project_id, title, goal, status, branch, base_branch,
          version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, NULL, ?, 1, ?, ?)
        """,
        (project_id, title, goal, status, base_branch, now, now),
    )
    conn.commit()
    return get_epic(conn, cur.lastrowid)


def get_epic(conn: sqlite3.Connection, epic_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM epics WHERE id = ?", (epic_id,)
    ).fetchone()
    if row is None:
        raise StoreError(f"epic {format_epic_id(epic_id)} not found")
    return row


def list_epics(
    conn: sqlite3.Connection, project_id: int | None = None
) -> list[sqlite3.Row]:
    if project_id is None:
        return list(
            conn.execute("SELECT * FROM epics ORDER BY id")
        )
    return list(
        conn.execute(
            "SELECT * FROM epics WHERE project_id = ? ORDER BY id",
            (project_id,),
        )
    )


def approve_epic(conn: sqlite3.Connection, epic_id: int) -> sqlite3.Row:
    """DB-only approve: scope approval or final review."""
    epic = get_epic(conn, epic_id)
    status = epic["status"]
    now = _now()
    if status == "awaiting_approval":
        conn.execute(
            """
            UPDATE epics SET status = 'executing', branch = COALESCE(branch, ?),
              updated_at = ? WHERE id = ?
            """,
            (f"teams/E-{epic_id}", now, epic_id),
        )
        conn.execute(
            """
            UPDATE tasks SET status = 'backlog', updated_at = ?, version = version + 1
            WHERE epic_id = ? AND status = 'draft'
            """,
            (now, epic_id),
        )
        _resolve_open_epic_approval(
            conn, epic_id, epic["project_id"], decision="approved", now=now
        )
        conn.commit()
        return get_epic(conn, epic_id)
    if status == "awaiting_review":
        conn.execute(
            "UPDATE epics SET status = 'done', updated_at = ? WHERE id = ?",
            (now, epic_id),
        )
        _resolve_open_epic_approval(
            conn, epic_id, epic["project_id"], decision="approved", now=now
        )
        conn.commit()
        return get_epic(conn, epic_id)
    raise StoreError(
        f"epic {format_epic_id(epic_id)} status is {status!r}; "
        "approve only from awaiting_approval or awaiting_review"
    )


def abandon_epic(conn: sqlite3.Connection, epic_id: int) -> sqlite3.Row:
    epic = get_epic(conn, epic_id)
    if epic["status"] in ("done", "abandoned"):
        raise StoreError(
            f"epic {format_epic_id(epic_id)} is already {epic['status']}"
        )
    now = _now()
    conn.execute(
        "UPDATE epics SET status = 'abandoned', updated_at = ? WHERE id = ?",
        (now, epic_id),
    )
    _resolve_open_epic_approval(
        conn,
        epic_id,
        epic["project_id"],
        decision="rejected",
        now=now,
        note="abandoned",
    )
    conn.commit()
    return get_epic(conn, epic_id)


def _resolve_open_epic_approval(
    conn: sqlite3.Connection,
    epic_id: int,
    project_id: int,
    *,
    decision: str,
    now: str,
    note: str | None = None,
) -> None:
    row = conn.execute(
        """
        SELECT id FROM approvals
        WHERE epic_id = ? AND resolved_at IS NULL
        """,
        (epic_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        """
        UPDATE approvals
        SET resolved_at = ?, applied_at = ?, decision = ?, human_note = ?
        WHERE id = ?
        """,
        (now, now, decision, note, row["id"]),
    )


# --- tasks ---


def create_task(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    title: str,
    body: str = "",
    epic_id: int | None = None,
    status: str = "backlog",
    stage: str | None = None,
    role: str | None = None,
    priority: int = 0,
    blocked_by: list[int] | None = None,
) -> sqlite3.Row:
    if status not in TASK_STATUSES:
        raise StoreError(f"invalid task status: {status}")
    if epic_id is not None:
        get_epic(conn, epic_id)
    now = _now()
    blocked_json = json.dumps(blocked_by or [])
    cur = conn.execute(
        """
        INSERT INTO tasks(
          project_id, epic_id, title, body, status, stage, role,
          priority, blocked_by, gate, version, loop_count,
          branch, worktree_path, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 0, NULL, NULL, ?, ?)
        """,
        (
            project_id,
            epic_id,
            title,
            body,
            status,
            stage,
            role,
            priority,
            blocked_json,
            now,
            now,
        ),
    )
    conn.commit()
    return get_task(conn, cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise StoreError(f"task {format_task_id(task_id)} not found")
    return row


def list_tasks(
    conn: sqlite3.Connection,
    *,
    project_id: int | None = None,
    epic_id: int | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if epic_id is not None:
        clauses.append("epic_id = ?")
        params.append(epic_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return list(
        conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority DESC, id",
            params,
        )
    )


def update_task_from_edit(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    title: str,
    body: str,
    status: str,
    stage: str | None,
    role: str | None,
    priority: int,
    blocked_by: list[int] | None,
    gate: str | None = None,
) -> sqlite3.Row:
    if status not in TASK_STATUSES:
        raise StoreError(f"invalid task status: {status}")
    task = get_task(conn, task_id)
    now = _now()
    conn.execute(
        """
        UPDATE tasks SET
          title = ?, body = ?, status = ?, stage = ?, role = ?,
          priority = ?, blocked_by = ?, gate = ?,
          version = version + 1, updated_at = ?
        WHERE id = ?
        """,
        (
            title,
            body,
            status,
            stage,
            role,
            priority,
            json.dumps(blocked_by or []),
            gate if gate is not None else task["gate"],
            now,
            task_id,
        ),
    )
    conn.commit()
    return get_task(conn, task_id)


# --- status overview ---


def status_overview(
    conn: sqlite3.Connection, project_id: int
) -> dict[str, Any]:
    epic_counts: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT status, COUNT(*) AS n FROM epics
        WHERE project_id = ? GROUP BY status
        """,
        (project_id,),
    ):
        epic_counts[row["status"]] = row["n"]

    task_counts: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT status, COUNT(*) AS n FROM tasks
        WHERE project_id = ? GROUP BY status
        """,
        (project_id,),
    ):
        task_counts[row["status"]] = row["n"]

    in_flight = list(
        conn.execute(
            """
            SELECT id, title, stage, role FROM tasks
            WHERE project_id = ? AND status = 'running'
            ORDER BY id
            """,
            (project_id,),
        )
    )

    queued_by_stage: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT COALESCE(stage, '(none)') AS stage, COUNT(*) AS n
        FROM tasks
        WHERE project_id = ? AND status = 'ready'
        GROUP BY stage
        ORDER BY stage
        """,
        (project_id,),
    ):
        queued_by_stage[row["stage"]] = row["n"]

    return {
        "epic_counts": epic_counts,
        "task_counts": task_counts,
        "in_flight": in_flight,
        "queued_by_stage": queued_by_stage,
    }


def task_edit_document(row: sqlite3.Row) -> str:
    """YAML frontmatter + markdown body for $EDITOR round-trip."""
    blocked = []
    if row["blocked_by"]:
        try:
            blocked = json.loads(row["blocked_by"])
        except json.JSONDecodeError as exc:
            raise ConfigError("blocked_by is not valid JSON") from exc

    meta = {
        "title": row["title"],
        "status": row["status"],
        "priority": row["priority"],
        "stage": row["stage"],
        "role": row["role"],
        "blocked_by": blocked,
        "epic_id": row["epic_id"],
        "version": row["version"],
        "loop_count": row["loop_count"],
    }
    # drop nulls for cleaner edit
    meta = {k: v for k, v in meta.items() if v is not None}
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    return f"---\n{fm}---\n\n{row['body']}"


def parse_task_edit_document(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        raise ConfigError("task edit must start with YAML frontmatter (---)")
    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        raise ConfigError("missing closing --- for frontmatter")
    fm_text = rest[:end]
    body = rest[end + 4 :]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    meta = yaml.safe_load(fm_text) or {}
    if not isinstance(meta, dict):
        raise ConfigError("frontmatter must be a mapping")
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ConfigError("title is required in frontmatter")
    status = meta.get("status", "backlog")
    if status not in TASK_STATUSES:
        raise ConfigError(f"invalid status: {status}")
    priority = meta.get("priority", 0)
    if not isinstance(priority, int):
        raise ConfigError("priority must be an int")
    blocked = meta.get("blocked_by") or []
    if not isinstance(blocked, list):
        raise ConfigError("blocked_by must be a list")
    blocked_ids: list[int] = []
    for entry in blocked:
        try:
            blocked_ids.append(int(entry))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"blocked_by entries must be task ids, got {entry!r}"
            ) from exc
    return {
        "title": title.strip(),
        "body": body,
        "status": status,
        "stage": meta.get("stage"),
        "role": meta.get("role"),
        "priority": priority,
        "blocked_by": blocked_ids,
    }
