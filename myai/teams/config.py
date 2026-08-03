from __future__ import annotations

import json
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from myai.paths import teams_prompts_dir, teams_root

EPIC_STATUSES = frozenset(
    {
        "grooming",
        "awaiting_approval",
        "executing",
        "awaiting_review",
        "done",
        "abandoned",
    }
)

TASK_STATUSES = frozenset(
    {
        "draft",
        "backlog",
        "ready",
        "running",
        "waiting_human",
        "blocked",
        "done",
        "failed",
    }
)

GATE_VALUES = frozenset({"auto", "human", "epic"})

DEFAULT_PIPELINE: list[dict[str, Any]] = [
    {"stage": "design", "role": "designer", "gate": "epic"},
    {"stage": "develop", "role": "developer", "gate": "auto"},
    {"stage": "qa", "role": "qa", "on_fail": "develop", "max_loops": 3},
    {"stage": "review", "gate": "epic"},
]

PROMPT_FILES = (
    "pm.md",
    "pm-groom.md",
    "designer.md",
    "developer.md",
    "qa.md",
)


class ConfigError(ValueError):
    pass


def prompt_path(name: str) -> str:
    """Relative path for a stock prompt under the teams state root."""
    return f"prompts/{name}"


def resolve_prompt_path(
    path: str | Path, *, workspace_path: str | Path | None = None
) -> Path:
    """Resolve a roster prompt path to an absolute Path.

    Absolute paths are used as-is. Paths under ``.myai/`` resolve against
    ``workspace_path``. Everything else is relative to ``teams_root()``
    (so ``prompts/pm.md`` → XDG ``…/teams/prompts/pm.md``).
    """
    p = Path(path)
    if p.is_absolute():
        return p
    text = p.as_posix()
    if text.startswith(".myai/") or text == ".myai":
        if workspace_path is None:
            raise ConfigError(
                f"prompt path {text!r} is project-local; workspace_path required"
            )
        return (Path(workspace_path) / p).resolve()
    return (teams_root() / p).resolve()


def default_roster() -> dict[str, Any]:
    return {
        "pm": {
            "backend": "cursor",
            "model_hint": None,
            "prompt": prompt_path("pm.md"),
            "groom_prompt": prompt_path("pm-groom.md"),
        },
        "designer": {
            "backend": "cursor",
            "prompt": prompt_path("designer.md"),
        },
        "developer": {
            "backend": "cursor",
            "prompt": prompt_path("developer.md"),
        },
        "qa": {
            "backend": "cursor",
            "prompt": prompt_path("qa.md"),
        },
    }


def default_config() -> dict[str, Any]:
    """Full default project config (concurrency defaults applied)."""
    cfg = {
        "roster": default_roster(),
        "pipeline": deepcopy(DEFAULT_PIPELINE),
        "concurrency": {
            "per_stage": {s["stage"]: 1 for s in DEFAULT_PIPELINE},
            "pm": 1,
        },
        "budgets": {"max_runs_per_task": 20},
        "epic_checks": None,
        "standups": [{"schedule": "0 9 * * *", "channel": "terminal"}],
        "notifications": {"inbox": ["terminal"]},
    }
    return cfg


def apply_concurrency_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """Fill omitted concurrency: per-stage 1, max_total unset, pm 1."""
    out = deepcopy(config)
    pipeline = out.get("pipeline") or []
    stages = [
        s["stage"]
        for s in pipeline
        if isinstance(s, dict) and isinstance(s.get("stage"), str)
    ]
    conc = out.get("concurrency")
    if not isinstance(conc, dict):
        conc = {}
    else:
        conc = dict(conc)
    per_stage = conc.get("per_stage")
    if not isinstance(per_stage, dict):
        per_stage = {}
    else:
        per_stage = dict(per_stage)
    for stage in stages:
        if stage not in per_stage:
            per_stage[stage] = 1
    conc["per_stage"] = per_stage
    if "pm" not in conc:
        conc["pm"] = 1
    # max_total left unset when omitted
    out["concurrency"] = conc
    return out


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError("config must be a mapping")
    roster = config.get("roster")
    if not isinstance(roster, dict) or not roster:
        raise ConfigError("roster is required")
    for role, entry in roster.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"roster.{role} must be a mapping")
        if "backend" not in entry:
            raise ConfigError(f"roster.{role}.backend is required")
        if "prompt" not in entry:
            raise ConfigError(f"roster.{role}.prompt is required")

    pipeline = config.get("pipeline")
    if not isinstance(pipeline, list) or not pipeline:
        raise ConfigError("pipeline must be a non-empty list")
    stage_names: list[str] = []
    for i, stage in enumerate(pipeline):
        if not isinstance(stage, dict):
            raise ConfigError(f"pipeline[{i}] must be a mapping")
        name = stage.get("stage")
        if not name:
            raise ConfigError(f"pipeline[{i}].stage is required")
        if not isinstance(name, str):
            raise ConfigError(f"pipeline[{i}].stage must be a string")
        if name in stage_names:
            raise ConfigError(f"duplicate pipeline stage: {name!r}")
        stage_names.append(name)
        gate = stage.get("gate")
        if gate is not None and gate not in GATE_VALUES:
            raise ConfigError(
                f"pipeline[{i}].gate must be one of {sorted(GATE_VALUES)}"
            )
        role = stage.get("role")
        if role is not None:
            if not isinstance(role, str):
                raise ConfigError(f"pipeline[{i}].role must be a string")
            if role not in roster:
                raise ConfigError(
                    f"pipeline[{i}].role {role!r} is not in roster"
                )

    stage_set = set(stage_names)
    for i, stage in enumerate(pipeline):
        on_fail = stage.get("on_fail")
        if on_fail is None:
            continue
        if not isinstance(on_fail, str):
            raise ConfigError(f"pipeline[{i}].on_fail must be a string")
        if on_fail not in stage_set:
            raise ConfigError(
                f"pipeline[{i}].on_fail {on_fail!r} is not a pipeline stage"
            )

    conc = config.get("concurrency")
    if conc is not None and not isinstance(conc, dict):
        raise ConfigError("concurrency must be a mapping")
    if isinstance(conc, dict):
        per = conc.get("per_stage")
        if per is not None:
            if not isinstance(per, dict):
                raise ConfigError("concurrency.per_stage must be a mapping")
            for stage_name, limit in per.items():
                if not isinstance(limit, int):
                    raise ConfigError(
                        f"concurrency.per_stage.{stage_name} must be an int"
                    )
        if "pm" in conc and not isinstance(conc["pm"], int):
            raise ConfigError("concurrency.pm must be an int")
        if "max_total" in conc and conc["max_total"] is not None:
            if not isinstance(conc["max_total"], int):
                raise ConfigError("concurrency.max_total must be an int")

    budgets = config.get("budgets")
    if budgets is not None and not isinstance(budgets, dict):
        raise ConfigError("budgets must be a mapping")

    return apply_concurrency_defaults(config)


def config_to_yaml(config: dict[str, Any]) -> str:
    return yaml.safe_dump(
        config,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def config_from_yaml(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("YAML root must be a mapping")
    return validate_config(data)


def config_to_json(config: dict[str, Any]) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def config_from_json(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ConfigError("config_json must be an object")
    return validate_config(data)


def bundled_prompts_dir():
    """Traversable for packaged prompt seeds (importlib.resources)."""
    return resources.files("myai.teams.prompts")


def install_default_prompts(*, overwrite: bool = False) -> list[Path]:
    """Copy bundled prompt seeds into XDG prompts dir. Returns written paths."""
    dest_root = teams_prompts_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    src_root = bundled_prompts_dir()
    written: list[Path] = []
    for name in PROMPT_FILES:
        src = src_root / name
        dest = dest_root / name
        if dest.exists() and not overwrite:
            continue
        if not src.is_file():
            raise ConfigError(f"missing bundled prompt: {name}")
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(dest)
    return written
