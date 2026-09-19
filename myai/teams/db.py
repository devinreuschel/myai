from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from myai.paths import (
    teams_artifacts_dir,
    teams_bots_dir,
    teams_daemon_lock_path,
    teams_db_path,
)

BUSY_TIMEOUT_MS = 5000

# (version, resource filename under myai.teams.migrations)
_MIGRATIONS: list[tuple[int, str]] = [
    (1, "001_initial.sql"),
]


class TeamsDBError(Exception):
    pass


def ensure_state_dirs() -> None:
    """Create XDG teams dirs and a placeholder daemon.lock."""
    for path in (teams_bots_dir(), teams_artifacts_dir()):
        path.mkdir(parents=True, exist_ok=True)
    lock = teams_daemon_lock_path()
    if not lock.exists():
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path if db_path is not None else teams_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout before WAL — the journal_mode switch takes a brief exclusive lock
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY)"
    )
    conn.commit()
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations")
    }
    pkg = resources.files("myai.teams.migrations")
    for version, filename in _MIGRATIONS:
        if version in applied:
            continue
        sql = (pkg / filename).read_text(encoding="utf-8")
        # Schema change and its version row commit together, so an interrupted
        # migration replays cleanly instead of leaving tables behind that the
        # next run trips over. executescript implicitly commits any pending
        # transaction, so BEGIN/COMMIT have to live inside the script itself —
        # which means migration files must not carry their own.
        script = (
            f"BEGIN;\n{sql}\n"
            f"INSERT INTO schema_migrations(version) VALUES ({version:d});\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            conn.rollback()
            raise


def move_aside_pre_pivot(db_path: Path | None = None) -> Path | None:
    """Rename a teams.db left by the pipeline design; return where it went.

    That schema shares migration version numbers with this one, so it cannot be
    migrated forward. Nothing shipped on it, so it is kept, not converted.
    """
    path = db_path if db_path is not None else teams_db_path()
    if not path.is_file():
        return None
    conn = sqlite3.connect(str(path))
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
        ).fetchone()
        if found is None:
            return None
        # fold the WAL back in so the renamed file is complete on its own
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.pre-pivot-{stamp}")
    path.rename(dest)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.rename(dest.with_name(dest.name + suffix))
    return dest


def open_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Ensure dirs, connect, and migrate. Caller owns the connection."""
    ensure_state_dirs()
    move_aside_pre_pivot(db_path)
    conn = connect(db_path)
    migrate(conn)
    return conn
