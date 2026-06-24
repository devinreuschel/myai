import json
from dataclasses import dataclass

from myai.paths import global_config_path


CONFIG_VERSION = 1


class GlobalConfigError(Exception):
    pass


@dataclass
class GlobalConfig:
    """User settings stored in ~/.myai/config.json."""

    version: int = CONFIG_VERSION
    inject_myai_rule: bool = True


def load_global_config() -> GlobalConfig:
    path = global_config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GlobalConfigError(f"invalid config at {path}: {exc}") from exc
        return GlobalConfig(
            version=data.get("version", CONFIG_VERSION),
            inject_myai_rule=data.get("inject_myai_rule", True),
        )
    return _default_from_legacy_registry()


def save_global_config(cfg: GlobalConfig) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": cfg.version,
        "inject_myai_rule": cfg.inject_myai_rule,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def get_inject_myai_rule_default() -> bool:
    return load_global_config().inject_myai_rule


def set_inject_myai_rule_default(enabled: bool) -> None:
    cfg = load_global_config()
    cfg.inject_myai_rule = enabled
    save_global_config(cfg)
    _clear_legacy_registry_inject_myai_rule()


def _default_from_legacy_registry() -> GlobalConfig:
    """Read inject_myai_rule from agentsync.json when ~/.myai/config.json is absent."""
    from myai.agentsync.registry import load

    data = load()
    if "inject_myai_rule" in data:
        return GlobalConfig(inject_myai_rule=bool(data["inject_myai_rule"]))
    return GlobalConfig()


def _clear_legacy_registry_inject_myai_rule() -> None:
    from myai.agentsync.registry import load, save

    data = load()
    if "inject_myai_rule" not in data:
        return
    del data["inject_myai_rule"]
    save(data)
