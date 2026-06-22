import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path

from myai.paths import sandbox_sessions_path


class SessionError(Exception):
    pass


@dataclass
class RepoSession:
    repo_path: str
    session_id: str | None = None
    resume_id: str | None = None
    alive: bool = False
    label: str = ""


def load_sessions() -> list[RepoSession]:
    path = sandbox_sessions_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SessionError(f"invalid sessions file at {path}: {exc}") from exc
    sessions = data.get("sessions", [])
    out: list[RepoSession] = []
    for item in sessions:
        out.append(
            RepoSession(
                repo_path=item.get("repo_path", ""),
                session_id=item.get("session_id"),
                resume_id=item.get("resume_id"),
                alive=bool(item.get("alive", False)),
                label=item.get("label", ""),
            )
        )
    return out


def save_session(session: RepoSession) -> None:
    sessions = load_sessions()
    replaced = False
    for i, existing in enumerate(sessions):
        if existing.repo_path == session.repo_path:
            sessions[i] = session
            replaced = True
            break
    if not replaced:
        sessions.append(session)
    _write_sessions(sessions)


def remove_session(repo_path: str) -> None:
    sessions = [s for s in load_sessions() if s.repo_path != repo_path]
    _write_sessions(sessions)


def list_repo_sessions() -> list[RepoSession]:
    return load_sessions()


def _write_sessions(sessions: list[RepoSession]) -> None:
    path = sandbox_sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sessions": [
            {
                "repo_path": s.repo_path,
                "session_id": s.session_id,
                "resume_id": s.resume_id,
                "alive": s.alive,
                "label": s.label,
            }
            for s in sessions
        ]
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def acquire_lock(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise SessionError(f"another sandbox is running for this repo ({lock_path})") from exc
    # keep fd open for process lifetime via module-level store
    _LOCK_FDS[lock_path] = fd


def release_lock(lock_path: Path) -> None:
    fd = _LOCK_FDS.pop(lock_path, None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


_LOCK_FDS: dict[Path, int] = {}
