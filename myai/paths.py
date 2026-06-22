import os
from pathlib import Path


def state_root() -> Path:
    if home := os.environ.get("MYAI_HOME"):
        return Path(home).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "myai").resolve()


def global_myai_dir() -> Path:
    return Path.home() / ".myai"


def agentsync_registry_path() -> Path:
    return state_root() / "agentsync.json"


def sandbox_root() -> Path:
    return state_root() / "sandbox"


def sandbox_sessions_path() -> Path:
    return sandbox_root() / "sessions.json"


def sandbox_locks_dir() -> Path:
    return sandbox_root() / "locks"


def global_sandbox_config_path() -> Path | None:
    """Return the first existing global sandbox config path, or preferred write path."""
    candidates = [
        global_myai_dir() / "sandbox.json",
        sandbox_root() / "sandbox.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def global_sandbox_config_write_path() -> Path:
    return global_myai_dir() / "sandbox.json"
