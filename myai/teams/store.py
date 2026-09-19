from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import yaml

from myai.teams.config import TASK_STATUSES, ConfigError
from myai.teams.db import TeamsDBError
from myai.teams.ids import format_task_id

# Lower wakes sooner: the human first, then the bot's own jobs, then other bots.
MAILBOX_PRIORITY = {
    "user_message": 0,
    "job_event": 1,
    "bot_message": 2,
    "schedule": 3,
}

_CLOSED_STATUSES = ("done", "dropped")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StoreError(TeamsDBError):
    pass


@contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[None]:
    """Commit the block as one transaction, so a change and its events land together."""
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _event(conn: sqlite3.Connection, kind: str, /, **payload: Any) -> None:
    conn.execute(
        "INSERT INTO events(ts, kind, payload_json) VALUES (?, ?, ?)",
        (_now(), kind, json.dumps(payload)),
    )


# --- principals ---


def get_principal(conn: sqlite3.Connection, principal_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM principals WHERE id = ?", (principal_id,)
    ).fetchone()
    if row is None:
        raise StoreError(f"no user or bot with id {principal_id!r}")
    return row


def get_user(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM principals WHERE kind = 'user' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        raise StoreError("no user; run `myai teams init`")
    return row


def create_user(conn: sqlite3.Connection, *, user_id: str, name: str) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT id FROM principals WHERE kind = 'user'"
    ).fetchone()
    if existing is not None:
        raise StoreError(f"teams is already initialized (user {existing['id']})")
    with _txn(conn):
        conn.execute(
            "INSERT INTO principals(id, kind, name, created_at) VALUES (?, 'user', ?, ?)",
            (user_id, name, _now()),
        )
        _event(conn, "principal_created", id=user_id, kind="user")
    return get_principal(conn, user_id)


def list_bots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM principals WHERE kind = 'bot' ORDER BY created_at, id")
    )


def resolve_bot(conn: sqlite3.Connection, id_or_none: str | None) -> sqlite3.Row:
    if id_or_none:
        row = get_principal(conn, id_or_none)
        if row["kind"] != "bot":
            raise StoreError(f"{id_or_none!r} is not a bot")
        return row
    rows = list_bots(conn)
    if not rows:
        raise StoreError("no bots; run `myai teams bot new`")
    if len(rows) > 1:
        raise StoreError("multiple bots; pass a bot id")
    return rows[0]


def create_bot(conn: sqlite3.Connection, *, bot_id: str, name: str) -> sqlite3.Row:
    """Register a bot, put the user in its address book, and open their DM."""
    user = get_user(conn)
    now = _now()
    try:
        with _txn(conn):
            conn.execute(
                "INSERT INTO principals(id, kind, name, created_at) VALUES (?, 'bot', ?, ?)",
                (bot_id, name, now),
            )
            conn.execute(
                "INSERT INTO contacts(bot_id, contact_id) VALUES (?, ?)",
                (bot_id, user["id"]),
            )
            cur = conn.execute(
                "INSERT INTO conversations(kind, created_at) VALUES ('dm', ?)", (now,)
            )
            conn.executemany(
                "INSERT INTO participants(conversation_id, principal_id, joined_at) "
                "VALUES (?, ?, ?)",
                [(cur.lastrowid, user["id"], now), (cur.lastrowid, bot_id, now)],
            )
            _event(conn, "principal_created", id=bot_id, kind="bot")
            _event(conn, "conversation_created", id=cur.lastrowid, kind="dm")
    except sqlite3.IntegrityError as exc:
        raise StoreError(f"id {bot_id!r} is already taken") from exc
    return get_principal(conn, bot_id)


def rename_principal(conn: sqlite3.Connection, principal_id: str, name: str) -> None:
    with _txn(conn):
        conn.execute(
            "UPDATE principals SET name = ? WHERE id = ?", (name, principal_id)
        )
        _event(conn, "principal_renamed", id=principal_id, name=name)


# --- conversations and messages ---


def get_dm(conn: sqlite3.Connection, a: str, b: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT c.* FROM conversations c
        JOIN participants pa ON pa.conversation_id = c.id AND pa.principal_id = ?
        JOIN participants pb ON pb.conversation_id = c.id AND pb.principal_id = ?
        WHERE c.kind = 'dm'
        """,
        (a, b),
    ).fetchone()
    if row is None:
        raise StoreError(f"no DM between {a!r} and {b!r}")
    return row


def _participant_ids(conn: sqlite3.Connection, conversation_id: int) -> set[str]:
    return {
        row["principal_id"]
        for row in conn.execute(
            "SELECT principal_id FROM participants WHERE conversation_id = ?",
            (conversation_id,),
        )
    }


def _post_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    sender_id: str,
    body: str,
    recipients: list[str],
    task_id: int | None,
) -> int:
    sender = get_principal(conn, sender_id)
    members = _participant_ids(conn, conversation_id)
    if sender_id not in members:
        raise StoreError(f"{sender_id!r} is not in conversation {conversation_id}")
    targets = list(dict.fromkeys(recipients))
    if sender_id in targets:
        raise StoreError("a message cannot be addressed to its sender")
    outsiders = [r for r in targets if r not in members]
    if outsiders:
        raise StoreError(f"recipients not in conversation {conversation_id}: {outsiders}")
    if sender["kind"] == "bot":
        allowed = {
            row["contact_id"]
            for row in conn.execute(
                "SELECT contact_id FROM contacts WHERE bot_id = ?", (sender_id,)
            )
        }
        blocked = [r for r in targets if r not in allowed]
        if blocked:
            raise StoreError(f"{sender_id!r} has no contact entry for {blocked}")

    now = _now()
    cur = conn.execute(
        "INSERT INTO messages(conversation_id, task_id, sender_id, body, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversation_id, task_id, sender_id, body, now),
    )
    message_id = cur.lastrowid
    kind = "user_message" if sender["kind"] == "user" else "bot_message"
    for recipient in targets:
        conn.execute(
            "INSERT INTO message_recipients(message_id, principal_id) VALUES (?, ?)",
            (message_id, recipient),
        )
        # only bots have a mailbox; a human reads the conversation
        if get_principal(conn, recipient)["kind"] == "bot":
            conn.execute(
                "INSERT INTO mailbox(bot_id, kind, ref_id, priority, enqueued_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (recipient, kind, message_id, MAILBOX_PRIORITY[kind], now),
            )
    _event(
        conn,
        "message_posted",
        id=message_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        task_id=task_id,
    )
    return message_id


def post_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    sender_id: str,
    body: str,
    recipients: list[str],
    task_id: int | None = None,
) -> sqlite3.Row:
    """Append a message; each addressed bot gets a mailbox item. Addressing wakes."""
    with _txn(conn):
        message_id = _post_message(
            conn,
            conversation_id=conversation_id,
            sender_id=sender_id,
            body=body,
            recipients=recipients,
            task_id=task_id,
        )
    return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()


def pending_mailbox(conn: sqlite3.Connection, bot_id: str) -> list[sqlite3.Row]:
    """Unfinished items in wake order."""
    return list(
        conn.execute(
            "SELECT * FROM mailbox WHERE bot_id = ? AND done_at IS NULL "
            "ORDER BY priority, id",
            (bot_id,),
        )
    )


# --- tasks ---


def create_task(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    title: str,
    created_by: str,
    owner_id: str | None = None,
    body: str = "",
) -> sqlite3.Row:
    """Open a task thread. The opening message is addressed to the owner."""
    if owner_id is not None and owner_id not in _participant_ids(conn, conversation_id):
        raise StoreError(f"owner {owner_id!r} is not in conversation {conversation_id}")
    now = _now()
    with _txn(conn):
        cur = conn.execute(
            """
            INSERT INTO tasks(
              conversation_id, title, owner_id, status, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'open', ?, ?, ?)
            """,
            (conversation_id, title, owner_id, created_by, now, now),
        )
        task_id = cur.lastrowid
        _event(conn, "task_created", id=task_id, owner_id=owner_id)
        recipients = [owner_id] if owner_id and owner_id != created_by else []
        _post_message(
            conn,
            conversation_id=conversation_id,
            sender_id=created_by,
            body=body or title,
            recipients=recipients,
            task_id=task_id,
        )
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise StoreError(f"task {format_task_id(task_id)} not found")
    return row


def list_tasks(
    conn: sqlite3.Connection,
    *,
    owner_id: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if owner_id is not None:
        clauses.append("owner_id = ?")
        params.append(owner_id)
    if status is not None:
        if status not in TASK_STATUSES:
            raise StoreError(f"invalid task status: {status}")
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return list(conn.execute(f"SELECT * FROM tasks {where} ORDER BY id", params))


def task_messages(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM messages WHERE task_id = ? ORDER BY id", (task_id,))
    )


def update_task_from_edit(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    title: str,
    status: str,
    owner_id: str | None,
    handoff: str,
    version: Any,
) -> sqlite3.Row:
    if status not in TASK_STATUSES:
        raise StoreError(f"invalid task status: {status}")
    task = get_task(conn, task_id)
    # the version in the edit doc is the one the editor opened on
    if version != task["version"]:
        raise StoreError(
            f"{format_task_id(task_id)} changed while it was being edited "
            f"(version {version!r} → {task['version']}); re-run the edit"
        )
    if owner_id is not None and owner_id not in _participant_ids(
        conn, task["conversation_id"]
    ):
        raise StoreError(f"owner {owner_id!r} is not in the task's conversation")
    with _txn(conn):
        conn.execute(
            """
            UPDATE tasks SET
              title = ?, status = ?, owner_id = ?, handoff = ?,
              version = version + 1, updated_at = ?
            WHERE id = ?
            """,
            (title, status, owner_id, handoff, _now(), task_id),
        )
        _event(conn, "task_updated", id=task_id, status=status, owner_id=owner_id)
    return get_task(conn, task_id)


def task_edit_document(row: sqlite3.Row) -> str:
    """YAML frontmatter + the handoff note as markdown, for the $EDITOR round-trip."""
    meta = {
        "title": row["title"],
        "status": row["status"],
        "owner": row["owner_id"],
        "version": row["version"],
    }
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    return f"---\n{fm}---\n\n{row['handoff']}"


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
    # the document puts a blank line after the frontmatter; don't grow the note by it
    body = rest[end + 4 :].lstrip("\r\n")
    meta = yaml.safe_load(fm_text) or {}
    if not isinstance(meta, dict):
        raise ConfigError("frontmatter must be a mapping")
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ConfigError("title is required in frontmatter")
    status = meta.get("status", "open")
    if status not in TASK_STATUSES:
        raise ConfigError(f"invalid status {status!r}; one of {list(TASK_STATUSES)}")
    owner = meta.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise ConfigError("owner must be a user or bot id, or null")
    return {
        "title": title.strip(),
        "status": status,
        "owner_id": owner,
        "handoff": body,
        "version": meta.get("version"),
    }


# --- status overview ---


def status_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    task_counts = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    }
    placeholders = ", ".join("?" for _ in _CLOSED_STATUSES)
    bots = []
    for bot in list_bots(conn):
        open_tasks = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE owner_id = ? "
            f"AND status NOT IN ({placeholders})",
            (bot["id"], *_CLOSED_STATUSES),
        ).fetchone()[0]
        bots.append(
            {
                "id": bot["id"],
                "name": bot["name"],
                "pending": len(pending_mailbox(conn, bot["id"])),
                "open_tasks": open_tasks,
            }
        )
    return {"bots": bots, "task_counts": task_counts}
