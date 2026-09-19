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


def sandbox_agent_git_dir(repo_digest: str) -> Path:
    """Per-repo scratch git dir the guest commits into (git_access='commit').

    Kept out of the workspace and out of the real .git, mounted writable into the
    guest; the host imports its refs after the run.
    """
    return sandbox_root() / "agent-git" / f"{repo_digest}.git"


def sandbox_trust_path() -> Path:
    """Approved repo sandbox configs. Kept out of sandbox/, whose subdirs get
    mounted into guests."""
    return state_root() / "sandbox-trust.json"


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


def global_config_path() -> Path:
    return global_myai_dir() / "config.json"


def global_sync_config_path() -> Path:
    """Selection for user-home agent sync (~/.claude, ~/.cursor, ~/.pi/agent)."""
    return global_myai_dir() / "global.json"


def global_sync_state_path() -> Path:
    """Tracked hashes for global home sync prune."""
    return state_root() / "global-state.json"


def teams_root() -> Path:
    return state_root() / "teams"


def teams_db_path() -> Path:
    return teams_root() / "teams.db"


def teams_daemon_lock_path() -> Path:
    return teams_root() / "daemon.lock"


def teams_bots_dir() -> Path:
    return teams_root() / "bots"


def teams_bot_home(bot_id: str) -> Path:
    return teams_bots_dir() / bot_id


def teams_artifacts_dir() -> Path:
    return teams_root() / "artifacts"
