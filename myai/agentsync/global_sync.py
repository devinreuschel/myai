import posixpath
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from myai.agentsync.config import ConfigError
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
    # Every home path is built from here, so validate traversal once, lexically.
    # Lexical (not resolve()) so legitimately symlinked agent homes still work.
    norm = posixpath.normpath(rel)
    if posixpath.isabs(norm) or norm == "." or norm == ".." or norm.startswith("../"):
        raise GlobalSyncError(f"unsafe global sync path {rel!r}")
    return agent, agent_home(agent), rel


def _home_for_state_key(key: str) -> tuple[str, Path, str] | None:
    """Like _home_for_key but tolerates junk in a hand-edited state file.

    Returns None for keys we refuse to touch, so a bad key gets dropped from
    state instead of wedging every future sync.
    """
    try:
        return _home_for_key(key)
    except (GlobalSyncError, ConfigError, ValueError):
        return None


def _clear_dest(path: Path) -> None:
    """Clear whatever is at path so a regular file can be written there."""
    if path.is_symlink():
        path.unlink()  # don't write through the link into its target
    elif path.is_dir():
        shutil.rmtree(path)


def _read_existing(home: Path, rel: str) -> str:
    path = home / rel
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


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
            # is_symlink covers dangling links, which would write to their target.
            if not path.exists() and not path.is_symlink():
                continue
            if key in old_state.files:
                continue
            if path.is_file() and sha256_file(path) == sha256_text(rendered.content):
                continue  # identical content, adopt it
            conflicts.append(key)
            continue

        if rendered.source_dir is None:
            continue

        # Skill dir copy replaces the whole tree (rmtree + copytree).
        if not path.exists() and not path.is_symlink():
            continue
        if not path.is_dir() or path.is_symlink():
            conflicts.append(key)
            continue

        planned = collect_skill_files(rendered.source_dir, rel)
        for existing in sorted(path.rglob("*")):
            if not existing.is_file():
                continue
            existing_rel = f"{rel}/{existing.relative_to(path).as_posix()}"
            if state_key(agent, existing_rel) in old_state.files:
                continue
            # Untracked file is only safe if master has identical content there;
            # anything else (extra file, different content) gets wiped.
            if planned.get(existing_rel) == sha256_file(existing):
                continue
            conflicts.append(key)
            break

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
            # Tolerate junk from a hand-edited state file; apply drops the key
            # without touching disk, so don't wedge compute (and thus status).
            resolved = _home_for_state_key(key)
            if resolved is None:
                continue
            _, home, rel = resolved
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
            agent = parse_state_key(key)[0]
            skill_hashes = collect_skill_files(rendered.source_dir, rel)
            changed = False
            for skill_rel, h in skill_hashes.items():
                skill_key = state_key(agent, skill_rel)
                path = home / skill_rel
                old_hash = sha256_file(path) if path.is_file() else None
                if old_hash != h:
                    changed = True
                new_state.files[skill_key] = h
            # The tree replace also wipes on-disk files master no longer has, so
            # those count as a change even when every master file already matches.
            dest = home / rel
            if dest.is_dir() and not dest.is_symlink():
                for existing in dest.rglob("*"):
                    if not existing.is_file():
                        continue
                    existing_rel = f"{rel}/{existing.relative_to(dest).as_posix()}"
                    if existing_rel not in skill_hashes:
                        changed = True
                        break
            elif dest.exists() or dest.is_symlink():
                changed = True
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

    # Track what actually landed; a failure part way through must not leave
    # written files untracked or deleted files still tracked.
    applied = GlobalSyncState(files=dict(old_state.files), blocks=dict(old_state.blocks))
    try:
        for key, block_content in blocks.items():
            _, home, rel = _home_for_key(key)
            path = home / rel
            existing = _read_existing(home, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inject_block(existing, block_content), encoding="utf-8")
            applied.blocks[key] = True

        for key in old_state.blocks:
            if key not in blocks:
                resolved = _home_for_state_key(key)
                if resolved is not None:
                    _, home, rel = resolved
                    path = home / rel
                    if path.is_file():
                        existing = path.read_text(encoding="utf-8")
                        path.write_text(inject_block(existing, ""), encoding="utf-8")
                applied.blocks.pop(key, None)

        for key, rendered in files.items():
            agent, home, rel = _home_for_key(key)
            path = home / rel
            if rendered.content is not None:
                _clear_dest(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered.content, encoding="utf-8")
                if key in new_state.files:
                    applied.files[key] = new_state.files[key]
            elif rendered.source_dir is not None:
                copy_skill_dir(rendered.source_dir, path)
                for skill_rel, h in collect_skill_files(rendered.source_dir, rel).items():
                    applied.files[state_key(agent, skill_rel)] = h

        skill_dirs: set[Path] = set()
        for key in old_state.files:
            if key in new_state.files:
                continue
            resolved = _home_for_state_key(key)
            if resolved is None:
                applied.files.pop(key, None)
                continue
            _, home, rel = resolved
            path = home / rel
            if path.is_symlink() or path.is_file():
                path.unlink()
                parts = Path(rel).parts
                if len(parts) >= 2 and parts[0] == "skills":
                    skill_dirs.add(home / parts[0] / parts[1])
            elif path.is_dir():
                shutil.rmtree(path)
            applied.files.pop(key, None)

        for skill_path in skill_dirs:
            if skill_path.is_dir() and not any(p.is_file() for p in skill_path.rglob("*")):
                shutil.rmtree(skill_path)
    finally:
        save_global_sync_state(applied)


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

        # State can change with no file actions (adopting identical untracked
        # content), so don't gate apply on actions alone.
        if not dry_run and (sync_plan.actions or sync_plan.new_state != old_state):
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
