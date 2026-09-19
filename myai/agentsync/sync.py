import posixpath
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from myai.agentsync.config import (
    RepoConfig,
    RepoState,
    load_config,
    load_state,
    resolve_inject_myai_rule,
    save_state,
)
from myai.agentsync.master import MasterError, resolve_selection
from myai.agentsync.render import (
    AGENT_CAPS,
    MYAI_APPEND_SYSTEM_REL,
    RenderPlan,
    build_plan,
    collect_skill_files,
    copy_skill_dir,
    inject_block,
    sha256_file,
    sha256_text,
)
from myai.agentsync.registry import get_master


class SyncError(Exception):
    pass


@dataclass
class SyncAction:
    kind: str  # write, delete, block
    path: str
    detail: str = ""


@dataclass
class SyncResult:
    repo: Path
    actions: list[SyncAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class SyncPlan:
    plan: RenderPlan
    actions: list[SyncAction]
    new_state: RepoState
    warnings: list[str] = field(default_factory=list)


class UnsafePathError(SyncError):
    pass


# A repo's .myai/state.json and its working tree arrive with the clone, so unlike
# the global plane nothing here is trusted: every path is checked before use.
_BLOCK_TARGETS = frozenset(caps.flat_target for caps in AGENT_CAPS.values())
_MANAGED_FILES = frozenset({MYAI_APPEND_SYSTEM_REL})
_MANAGED_DIRS = frozenset(
    {".claude/agents"}
    | {caps.nested_dir for caps in AGENT_CAPS.values() if caps.nested_dir}
    | {caps.skills_dir for caps in AGENT_CAPS.values() if caps.skills_dir}
)


def _check_rel(rel: str) -> None:
    """Reject anything but a canonical repo-relative path. Lexical only."""
    if (
        not isinstance(rel, str)
        or not rel
        or posixpath.isabs(rel)
        or "\\" in rel
        or posixpath.normpath(rel) != rel
        or rel in (".", "..")
        or rel.startswith("../")
    ):
        raise UnsafePathError(f"unsafe path {rel!r}")


def _is_managed(rel: str) -> bool:
    """True for paths sync itself could have written: inside a managed dir, not the dir."""
    if rel in _MANAGED_FILES:
        return True
    return any(rel.startswith(root + "/") for root in _MANAGED_DIRS)


def _inside(repo_root: Path, path: Path) -> bool:
    return path == repo_root or repo_root in path.parents


def _resolve_inside(repo: Path, rel: str) -> Path:
    """Path for rel with symlinked ancestors resolved; refuses to leave the repo.

    The leaf is deliberately not followed, so callers can unlink a symlink
    sitting at the destination instead of writing through it.
    """
    _check_rel(rel)
    root = repo.resolve()
    path = repo / rel
    parent = path.parent.resolve()
    if not _inside(root, parent):
        raise UnsafePathError(f"{rel!r} resolves outside the repo")
    return parent / path.name


def _resolve_block_target(repo: Path, rel: str) -> Path:
    """Where a managed block gets written. CLAUDE.md -> AGENTS.md style links are
    fine and written through; a link that leaves the repo is not."""
    if rel not in _BLOCK_TARGETS:
        raise UnsafePathError(f"{rel!r} is not a managed block target")
    path = _resolve_inside(repo, rel)
    if path.is_symlink():
        target = path.resolve()
        if not _inside(repo.resolve(), target):
            raise UnsafePathError(f"{rel!r} is a symlink that leaves the repo")
        return target
    return path


def _resolve_state_file(repo: Path, rel: str) -> Path:
    """Resolve a state-tracked path we may delete. Only ever inside managed dirs."""
    _check_rel(rel)
    if not _is_managed(rel):
        raise UnsafePathError(f"{rel!r} is outside the directories sync manages")
    return _resolve_inside(repo, rel)


def _clear_dest(path: Path) -> None:
    """Clear whatever is at path so a regular file can be written there."""
    if path.is_symlink():
        path.unlink()  # don't write through the link into its target
    elif path.is_dir():
        shutil.rmtree(path)


def _read_text(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def compute_sync(repo: Path, cfg: RepoConfig, old_state: RepoState) -> SyncPlan:
    master = get_master()
    if master is None:
        raise SyncError("no master repo registered; run myai master init")
    if not master.is_dir():
        raise SyncError(f"master repo not found at {master}")

    rules, skills, subagents = resolve_selection(
        master, cfg.rules, cfg.skills, cfg.subagents
    )
    plan = build_plan(
        repo,
        cfg.agents,
        rules,
        skills,
        subagents,
        cfg.nested_rules,
        inject_myai_rule=resolve_inject_myai_rule(cfg),
    )

    actions: list[SyncAction] = []
    warnings: list[str] = []
    new_state = RepoState(files={}, blocks={})

    # Everything we are about to write is resolved here, before apply touches
    # disk, so one bad target fails the whole repo instead of half of it.
    for rel, block_content in plan.blocks.items():
        existing = _read_text(_resolve_block_target(repo, rel))
        new_content = inject_block(existing, block_content)
        if sha256_text(existing) != sha256_text(new_content):
            actions.append(SyncAction("block", rel, "update managed block"))
        new_state.blocks[rel] = True

    for rel in old_state.blocks:
        if rel in plan.blocks:
            continue
        try:
            path = _resolve_block_target(repo, rel)
        except UnsafePathError as exc:
            warnings.append(f"ignored state entry: {exc}")
            continue
        existing = _read_text(path)
        if existing != inject_block(existing, ""):
            actions.append(SyncAction("block", rel, "remove managed block"))

    for rel, rendered in plan.files.items():
        path = _resolve_inside(repo, rel)
        if rendered.content is not None:
            new_hash = sha256_text(rendered.content)
            old_hash = (
                sha256_file(path) if path.is_file() and not path.is_symlink() else None
            )
            if old_hash != new_hash:
                actions.append(SyncAction("write", rel))
            new_state.files[rel] = new_hash
        elif rendered.source_dir is not None:
            skill_hashes = collect_skill_files(rendered.source_dir, rel)
            changed = path.is_symlink()
            for skill_rel, h in skill_hashes.items():
                skill_path = _resolve_inside(repo, skill_rel)
                old_hash = sha256_file(skill_path) if skill_path.is_file() else None
                if old_hash != h:
                    changed = True
                new_state.files[skill_rel] = h
            if changed:
                actions.append(SyncAction("write", rel, "sync skill directory"))

    for rel in old_state.files:
        if rel in new_state.files:
            continue
        try:
            _resolve_state_file(repo, rel)
        except UnsafePathError as exc:
            warnings.append(f"ignored state entry: {exc}")
            continue
        actions.append(SyncAction("delete", rel))

    return SyncPlan(plan=plan, actions=actions, new_state=new_state, warnings=warnings)


def apply_sync(repo: Path, sync_plan: SyncPlan, old_state: RepoState) -> None:
    plan = sync_plan.plan
    new_state = sync_plan.new_state

    for rel, block_content in plan.blocks.items():
        path = _resolve_block_target(repo, rel)
        existing = _read_text(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inject_block(existing, block_content), encoding="utf-8")

    for rel in old_state.blocks:
        if rel in plan.blocks:
            continue
        try:
            path = _resolve_block_target(repo, rel)
        except UnsafePathError:
            continue  # reported by compute_sync; never act on it
        if path.is_file():
            existing = path.read_text(encoding="utf-8")
            path.write_text(inject_block(existing, ""), encoding="utf-8")

    for rel, rendered in plan.files.items():
        path = _resolve_inside(repo, rel)
        if rendered.content is not None:
            _clear_dest(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered.content, encoding="utf-8")
        elif rendered.source_dir is not None:
            copy_skill_dir(rendered.source_dir, path)

    for rel in old_state.files:
        if rel in new_state.files:
            continue
        try:
            path = _resolve_state_file(repo, rel)
        except UnsafePathError:
            continue  # reported by compute_sync; never act on it
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    save_state(repo, new_state)


def sync_repo(repo: Path, dry_run: bool = False) -> SyncResult:
    result = SyncResult(repo=repo)
    try:
        cfg = load_config(repo)
        if not cfg.managed:
            result.error = "repo is not managed"
            return result
        old_state = load_state(repo)
        sync_plan = compute_sync(repo, cfg, old_state)
        result.actions = sync_plan.actions
        result.warnings = sync_plan.warnings
        # Warnings mean state held entries we refuse to act on; apply rewrites
        # state without them so they stop resurfacing on every sync.
        if not dry_run and (sync_plan.actions or sync_plan.warnings):
            apply_sync(repo, sync_plan, old_state)
        return result
    except (SyncError, MasterError) as exc:
        result.error = str(exc)
        return result
    except Exception as exc:
        result.error = str(exc)
        return result


def sync_all(repos: list[Path], dry_run: bool = False) -> list[SyncResult]:
    return [sync_repo(r, dry_run=dry_run) for r in repos]


def print_sync_result(result: SyncResult, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    for warning in result.warnings:
        print(f"{prefix}{result.repo}: warning: {warning}", file=sys.stderr)
    if result.error:
        print(f"{prefix}{result.repo}: error: {result.error}", file=sys.stderr)
        return
    if not result.actions:
        print(f"{prefix}{result.repo}: up to date")
        return
    print(f"{prefix}{result.repo}:")
    for action in result.actions:
        detail = f" ({action.detail})" if action.detail else ""
        print(f"  {action.kind}: {action.path}{detail}")
