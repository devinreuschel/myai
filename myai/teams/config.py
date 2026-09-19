from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from myai.paths import teams_bot_home

TASK_STATUSES = ("open", "active", "blocked", "needs_input", "done", "dropped")

PRESETS = ("hands-off", "supervised", "locked-down")

BOT_CONFIG_FILE = "bot.yaml"
_BOT_CONFIG_KEYS = ("name", "job", "preset", "budgets")
_DEFAULT_BUDGETS = {"wake_turns": 8, "wake_seconds": 60}


class ConfigError(ValueError):
    pass


def default_bot_config(name: str, job: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "job": job,
        "preset": "hands-off",
        "budgets": dict(_DEFAULT_BUDGETS),
    }


def validate_bot_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError("bot config must be a mapping")
    unknown = sorted(set(config) - set(_BOT_CONFIG_KEYS))
    if unknown:
        raise ConfigError(
            f"unknown bot config keys {unknown}; allowed: {list(_BOT_CONFIG_KEYS)}"
        )
    name = config.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("name is required")
    job = config.get("job", "")
    if not isinstance(job, str):
        raise ConfigError("job must be a string")
    preset = config.get("preset", "hands-off")
    if preset not in PRESETS:
        raise ConfigError(f"preset must be one of {list(PRESETS)}")
    budgets = config.get("budgets", {})
    if not isinstance(budgets, dict):
        raise ConfigError("budgets must be a mapping")
    unknown = sorted(set(budgets) - set(_DEFAULT_BUDGETS))
    if unknown:
        raise ConfigError(
            f"unknown budgets keys {unknown}; allowed: {list(_DEFAULT_BUDGETS)}"
        )
    for key, value in budgets.items():
        # bool is an int subclass; `wake_turns: true` is a typo, not a budget
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"budgets.{key} must be a positive int")
    return {
        "name": name.strip(),
        "job": job,
        "preset": preset,
        "budgets": {**_DEFAULT_BUDGETS, **budgets},
    }


def bot_config_to_yaml(config: dict[str, Any]) -> str:
    return yaml.safe_dump(
        config,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def bot_config_from_yaml(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    return validate_bot_config({} if data is None else data)


def ensure_bot_home(bot_id: str, config: dict[str, Any]) -> Path:
    """Create the bot's home layout; existing files are never overwritten."""
    home = teams_bot_home(bot_id)
    for sub in ("playbooks", "workspace"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    seeds = {
        BOT_CONFIG_FILE: bot_config_to_yaml(config),
        "persona.md": f"# {config['name']}\n\n{config['job']}\n",
        "constraints.md": "",
    }
    for filename, content in seeds.items():
        path = home / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    return home


def load_bot_config(bot_id: str) -> dict[str, Any]:
    path = teams_bot_home(bot_id) / BOT_CONFIG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return bot_config_from_yaml(text)
