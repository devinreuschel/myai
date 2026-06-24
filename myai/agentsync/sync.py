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
    error: str | None = None


@dataclass
class SyncPlan:
    plan: RenderPlan
    actions: list[SyncAction]
    new_state: RepoState


def _read_existing(repo: Path, rel: str) -> str:
    path = repo / rel
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
    new_state = RepoState(files={}, blocks={})

    for rel, block_content in plan.blocks.items():
        existing = _read_existing(repo, rel)
        new_content = inject_block(existing, block_content)
        if sha256_text(existing) != sha256_text(new_content):
            actions.append(SyncAction("block", rel, "update managed block"))
        new_state.blocks[rel] = True

    for rel in old_state.blocks:
        if rel not in plan.blocks:
            existing = _read_existing(repo, rel)
            new_content = inject_block(existing, "")
            if existing != new_content:
                actions.append(SyncAction("block", rel, "remove managed block"))

    for rel, rendered in plan.files.items():
        if rendered.content is not None:
            new_hash = sha256_text(rendered.content)
            path = repo / rel
            old_hash = sha256_file(path) if path.is_file() else None
            if old_hash != new_hash:
                actions.append(SyncAction("write", rel))
            new_state.files[rel] = new_hash
        elif rendered.source_dir is not None:
            skill_hashes = collect_skill_files(rendered.source_dir, rel)
            changed = False
            for skill_rel, h in skill_hashes.items():
                path = repo / skill_rel
                old_hash = sha256_file(path) if path.is_file() else None
                if old_hash != h:
                    changed = True
                new_state.files[skill_rel] = h
            if changed:
                actions.append(SyncAction("write", rel, "sync skill directory"))

    for rel in old_state.files:
        if rel not in new_state.files:
            actions.append(SyncAction("delete", rel))

    return SyncPlan(plan=plan, actions=actions, new_state=new_state)


def apply_sync(repo: Path, sync_plan: SyncPlan, old_state: RepoState) -> None:
    plan = sync_plan.plan
    new_state = sync_plan.new_state

    for rel, block_content in plan.blocks.items():
        path = repo / rel
        existing = _read_existing(repo, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inject_block(existing, block_content), encoding="utf-8")

    for rel in old_state.blocks:
        if rel not in plan.blocks:
            path = repo / rel
            if path.is_file():
                existing = path.read_text(encoding="utf-8")
                path.write_text(inject_block(existing, ""), encoding="utf-8")

    for rel, rendered in plan.files.items():
        path = repo / rel
        if rendered.content is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered.content, encoding="utf-8")
        elif rendered.source_dir is not None:
            copy_skill_dir(rendered.source_dir, path)

    for rel in old_state.files:
        if rel not in new_state.files:
            path = repo / rel
            if path.is_file():
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
        if not dry_run and sync_plan.actions:
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
