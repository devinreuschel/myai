import json
from dataclasses import dataclass, field

from myai.agentsync.config import AGENTS, ConfigError
from myai.global_config import get_inject_myai_rule_default
from myai.paths import global_sync_config_path, global_sync_state_path

CONFIG_VERSION = 1


@dataclass
class GlobalSyncConfig:
    """User-home agentsync selection stored in ~/.myai/global.json."""

    version: int = CONFIG_VERSION
    agents: list[str] = field(default_factory=lambda: list(AGENTS))
    rules: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    nested_rules: bool = True
    inject_myai_rule: bool | None = None

    def validate(self) -> None:
        for agent in self.agents:
            if agent not in AGENTS:
                raise ConfigError(f"unknown agent {agent!r}, expected one of {AGENTS}")


@dataclass
class GlobalSyncState:
    """Tracked files/blocks for global home prune. Keys are agent:relpath."""

    files: dict[str, str] = field(default_factory=dict)
    blocks: dict[str, bool] = field(default_factory=dict)


def config_exists() -> bool:
    return global_sync_config_path().is_file()


def load_global_sync_config() -> GlobalSyncConfig:
    path = global_sync_config_path()
    if not path.is_file():
        raise ConfigError(f"no global sync config at {path}; run myai global init")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid config at {path}: {exc}") from exc
    cfg = GlobalSyncConfig(
        version=data.get("version", CONFIG_VERSION),
        agents=list(data.get("agents", list(AGENTS))),
        rules=list(data.get("rules", [])),
        skills=list(data.get("skills", [])),
        subagents=list(data.get("subagents", [])),
        nested_rules=data.get("nested_rules", True),
        inject_myai_rule=data.get("inject_myai_rule"),
    )
    cfg.validate()
    return cfg


def save_global_sync_config(cfg: GlobalSyncConfig) -> None:
    cfg.validate()
    path = global_sync_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": cfg.version,
        "agents": cfg.agents,
        "rules": cfg.rules,
        "skills": cfg.skills,
        "subagents": cfg.subagents,
        "nested_rules": cfg.nested_rules,
    }
    if cfg.inject_myai_rule is not None:
        data["inject_myai_rule"] = cfg.inject_myai_rule
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def resolve_global_inject_myai_rule(cfg: GlobalSyncConfig) -> bool:
    if cfg.inject_myai_rule is not None:
        return cfg.inject_myai_rule
    return get_inject_myai_rule_default()


def load_global_sync_state() -> GlobalSyncState:
    path = global_sync_state_path()
    if not path.is_file():
        return GlobalSyncState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid state at {path}: {exc}") from exc
    return GlobalSyncState(
        files=dict(data.get("files", {})),
        blocks=dict(data.get("blocks", {})),
    )


def save_global_sync_state(state: GlobalSyncState) -> None:
    path = global_sync_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"files": state.files, "blocks": state.blocks}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def state_key(agent: str, rel: str) -> str:
    return f"{agent}:{rel}"


def parse_state_key(key: str) -> tuple[str, str]:
    agent, _, rel = key.partition(":")
    if not agent or not rel:
        raise ConfigError(f"invalid global state key {key!r}")
    return agent, rel
