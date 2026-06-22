import os
from pathlib import Path


def state_root() -> Path:
    if home := os.environ.get("MYAI_HOME"):
        return Path(home).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "myai").resolve()


def agentsync_registry_path() -> Path:
    return state_root() / "agentsync.json"
