import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_VERSION = 1
MYAI_DIR = ".myai"
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

AGENTS = ("cursor", "claude", "pi")


class ConfigError(Exception):
    pass


@dataclass
class RepoConfig:
    """Per-repo agentsync config stored in .myai/config.json."""

    version: int = CONFIG_VERSION
    managed: bool = True
    agents: list[str] = field(default_factory=lambda: list(AGENTS))
    rules: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    nested_rules: bool = True

    def validate(self) -> None:
        for agent in self.agents:
            if agent not in AGENTS:
                raise ConfigError(f"unknown agent {agent!r}, expected one of {AGENTS}")


@dataclass
class RepoState:
    files: dict[str, str] = field(default_factory=dict)
    blocks: dict[str, bool] = field(default_factory=dict)


def myai_dir(repo: Path) -> Path:
    return repo / MYAI_DIR


def config_path(repo: Path) -> Path:
    return myai_dir(repo) / CONFIG_FILE


def state_path(repo: Path) -> Path:
    return myai_dir(repo) / STATE_FILE


def load_config(repo: Path) -> RepoConfig:
    path = config_path(repo)
    if not path.is_file():
        raise ConfigError(f"no config at {path}; run myai init")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid config at {path}: {exc}") from exc
    cfg = RepoConfig(
        version=data.get("version", CONFIG_VERSION),
        managed=data.get("managed", True),
        agents=list(data.get("agents", list(AGENTS))),
        rules=list(data.get("rules", [])),
        skills=list(data.get("skills", [])),
        subagents=list(data.get("subagents", [])),
        nested_rules=data.get("nested_rules", True),
    )
    cfg.validate()
    return cfg


def save_config(repo: Path, cfg: RepoConfig) -> None:
    cfg.validate()
    path = config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": cfg.version,
        "managed": cfg.managed,
        "agents": cfg.agents,
        "rules": cfg.rules,
        "skills": cfg.skills,
        "subagents": cfg.subagents,
        "nested_rules": cfg.nested_rules,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_state(repo: Path) -> RepoState:
    path = state_path(repo)
    if not path.is_file():
        return RepoState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid state at {path}: {exc}") from exc
    return RepoState(
        files=dict(data.get("files", {})),
        blocks=dict(data.get("blocks", {})),
    )


def save_state(repo: Path, state: RepoState) -> None:
    path = state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"files": state.files, "blocks": state.blocks}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
