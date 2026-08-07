import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from myai.agentsync.global_config import (
    GlobalSyncConfig,
    GlobalSyncState,
    load_global_sync_config,
    load_global_sync_state,
    parse_state_key,
    resolve_global_inject_myai_rule,
    save_global_sync_state,
    state_key,
)
from myai.agentsync.global_homes import agent_home, build_global_plan
from myai.agentsync.master import MasterError, resolve_selection
from myai.agentsync.render import (
    RenderedFile,
    collect_skill_files,
    copy_skill_dir,
    inject_block,
    sha256_file,
    sha256_text,
)
from myai.agentsync.registry import get_master
from myai.agentsync.sync import SyncAction


class GlobalSyncError(Exception):
    pass


@dataclass
class GlobalSyncResult:
    actions: list[SyncAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class GlobalSyncPlan:
    actions: list[SyncAction]
    new_state: GlobalSyncState
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    files: dict[str, RenderedFile] = field(default_factory=dict, repr=False)
    blocks: dict[str, str] = field(default_factory=dict, repr=False)


def _home_for_key(key: str) -> tuple[str, Path, str]:
    agent, rel = parse_state_key(key)
    return agent, agent_home(agent), rel


def _read_existing(home: Path, rel: str) -> str:
    path = home / rel
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def _skill_file_keys(agent: str, skill_rel: str, skill_dir: Path) -> list[str]:
    """State keys for every file currently under an on-disk skill directory."""
    keys: list[str] = []
    if not skill_dir.is_dir():
        return keys
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            rel = f"{skill_rel}/{path.relative_to(skill_dir).as_posix()}"
            keys.append(state_key(agent, rel))
    return keys


def detect_clobber_conflicts(
    files: dict[str, RenderedFile],
    old_state: GlobalSyncState,
) -> list[str]:
    """Return agent:rel keys that would overwrite untracked user content.

    Tracked myai-managed paths may be updated without counting as a conflict.
    Managed-block injection into CLAUDE.md/AGENTS.md is additive and not listed.
    """
    conflicts: list[str] = []
    for key, rendered in files.items():
        agent, home, rel = _home_for_key(key)
        path = home / rel

        if rendered.content is not None:
            if not path.is_file():
                continue
            if key in old_state.files:
                continue
            new_hash = sha256_text(rendered.content)
            if sha256_file(path) != new_hash:
                conflicts.append(key)
            continue

        if rendered.source_dir is None:
            continue

        # Skill dir copy replaces the whole tree (rmtree + copytree).
        if not path.exists():
            continue
        if path.is_file():
            conflicts.append(key)
            continue
        if not path.is_dir():
            continue

        existing_keys = _skill_file_keys(agent, rel, path)
        if not existing_keys:
            continue
        if any(k not in old_state.files for k in existing_keys):
            conflicts.append(key)

    return sorted(set(conflicts))


def compute_global_sync(
    cfg: GlobalSyncConfig,
    old_state: GlobalSyncState,
) -> GlobalSyncPlan:
    master = get_master()
    if master is None:
        raise GlobalSyncError("no master repo registered; run myai master init")
    if not master.is_dir():
        raise GlobalSyncError(f"master repo not found at {master}")

    rules, skills, subagents = resolve_selection(
        master, cfg.rules, cfg.skills, cfg.subagents
    )
    plan = build_global_plan(
        cfg.agents,
        rules,
        skills,
        subagents,
        cfg.nested_rules,
        inject_myai_rule=resolve_global_inject_myai_rule(cfg),
    )

    actions: list[SyncAction] = []
    new_state = GlobalSyncState(files={}, blocks={})

    for key, block_content in plan.blocks.items():
        _, home, rel = _home_for_key(key)
        existing = _read_existing(home, rel)
        new_content = inject_block(existing, block_content)
        if sha256_text(existing) != sha256_text(new_content):
            actions.append(SyncAction("block", key, "update managed block"))
        new_state.blocks[key] = True

    for key in old_state.blocks:
        if key not in plan.blocks:
            _, home, rel = _home_for_key(key)
            existing = _read_existing(home, rel)
            new_content = inject_block(existing, "")
            if existing != new_content:
                actions.append(SyncAction("block", key, "remove managed block"))

    for key, rendered in plan.files.items():
        _, home, rel = _home_for_key(key)
        if rendered.content is not None:
            new_hash = sha256_text(rendered.content)
            path = home / rel
            old_hash = sha256_file(path) if path.is_file() else None
            if old_hash != new_hash:
                actions.append(SyncAction("write", key))
            new_state.files[key] = new_hash
        elif rendered.source_dir is not None:
            skill_hashes = collect_skill_files(rendered.source_dir, rel)
            changed = False
            for skill_rel, h in skill_hashes.items():
                skill_key = state_key(parse_state_key(key)[0], skill_rel)
                path = home / skill_rel
                old_hash = sha256_file(path) if path.is_file() else None
                if old_hash != h:
                    changed = True
                new_state.files[skill_key] = h
            if changed:
                actions.append(SyncAction("write", key, "sync skill directory"))

    for key in old_state.files:
        if key not in new_state.files:
            actions.append(SyncAction("delete", key))

    conflicts = detect_clobber_conflicts(plan.files, old_state)

    return GlobalSyncPlan(
        actions=actions,
        new_state=new_state,
        warnings=list(plan.warnings),
        conflicts=conflicts,
        files=plan.files,
        blocks=plan.blocks,
    )


def apply_global_sync(sync_plan: GlobalSyncPlan, old_state: GlobalSyncState) -> None:
    files = sync_plan.files
    blocks = sync_plan.blocks
    new_state = sync_plan.new_state

    for key, block_content in blocks.items():
        _, home, rel = _home_for_key(key)
        path = home / rel
        existing = _read_existing(home, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inject_block(existing, block_content), encoding="utf-8")

    for key in old_state.blocks:
        if key not in blocks:
            _, home, rel = _home_for_key(key)
            path = home / rel
            if path.is_file():
                existing = path.read_text(encoding="utf-8")
                path.write_text(inject_block(existing, ""), encoding="utf-8")

    for key, rendered in files.items():
        _, home, rel = _home_for_key(key)
        path = home / rel
        if rendered.content is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered.content, encoding="utf-8")
        elif rendered.source_dir is not None:
            copy_skill_dir(rendered.source_dir, path)

    skill_dirs: set[Path] = set()
    for key in old_state.files:
        if key in new_state.files:
            continue
        _, home, rel = _home_for_key(key)
        path = home / rel
        if path.is_file():
            path.unlink()
            parts = Path(rel).parts
            if len(parts) >= 2 and parts[0] == "skills":
                skill_dirs.add(home / parts[0] / parts[1])
        elif path.is_dir():
            shutil.rmtree(path)

    for skill_path in skill_dirs:
        if skill_path.is_dir() and not any(p.is_file() for p in skill_path.rglob("*")):
            shutil.rmtree(skill_path)

    save_global_sync_state(new_state)


def sync_global(dry_run: bool = False, allow_clobber: bool = False) -> GlobalSyncResult:
    """Apply global sync. Refuses clobber conflicts unless allow_clobber=True."""
    result = GlobalSyncResult()
    try:
        cfg = load_global_sync_config()
        old_state = load_global_sync_state()
        sync_plan = compute_global_sync(cfg, old_state)
        result.actions = sync_plan.actions
        result.warnings = sync_plan.warnings
        result.conflicts = list(sync_plan.conflicts)

        if sync_plan.conflicts and not dry_run and not allow_clobber:
            result.error = (
                "would overwrite existing untracked files in agent homes; "
                "review conflicts and re-run with -y to confirm"
            )
            return result

        if not dry_run and sync_plan.actions:
            apply_global_sync(sync_plan, old_state)
        return result
    except (GlobalSyncError, MasterError) as exc:
        result.error = str(exc)
        return result
    except Exception as exc:
        result.error = str(exc)
        return result


def format_conflict_paths(conflicts: list[str]) -> list[str]:
    """Human-readable absolute paths for conflict keys."""
    lines: list[str] = []
    for key in conflicts:
        _, home, rel = _home_for_key(key)
        lines.append(str(home / rel))
    return lines


def print_global_sync_result(result: GlobalSyncResult, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    if result.conflicts:
        print(f"{prefix}conflicts (would overwrite untracked files):", file=sys.stderr)
        for path in format_conflict_paths(result.conflicts):
            print(f"  {path}", file=sys.stderr)
    if result.error:
        print(f"{prefix}global: error: {result.error}", file=sys.stderr)
        return
    if not result.actions:
        print(f"{prefix}global: up to date")
        return
    print(f"{prefix}global:")
    for action in result.actions:
        detail = f" ({action.detail})" if action.detail else ""
        print(f"  {action.kind}: {action.path}{detail}")
