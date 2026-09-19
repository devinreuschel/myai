"""Scratch git dir for the sandboxed agent (git_access='commit').

The real ``.git`` stays read-only to the guest, so the agent commits into a
throwaway clone that borrows the repo's objects (read-only) and takes its own
writes. When the run ends, trusted host code imports the branches the agent
produced into ``refs/sandbox/<run>/*`` for review — the real repo never takes a
write from inside the sandbox.

Everything the guest writes into the scratch dir is untrusted: its hooks only
ever run inside the VM (``git fetch`` does not run a source repo's hooks), and
the object-store ``alternates`` pointer is rewritten to a known-good path before
the host reads it.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from myai.paths import sandbox_agent_git_dir
from myai.sandbox.config import (
    AGENT_GIT_MOUNT,
    SANDBOX_REF_NAMESPACE,
    SandboxConfig,
    effective_workspace_path,
    git_commit_mode,
)


class AgentGitError(Exception):
    pass


@dataclass(frozen=True)
class AgentGitLayout:
    host_git_dir: Path  # scratch dir on the host (mounted into the guest)
    guest_git_dir: str  # where it is mounted in the guest (GIT_DIR)
    guest_work_tree: str  # the repo mount (GIT_WORK_TREE)
    guest_objects: str  # guest-visible real .git/objects, for the alternate


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AgentGitError(f"git {' '.join(args)} failed: {detail}")
    return proc


def is_git_repo(repo: Path) -> bool:
    return (repo / ".git").is_dir()


def layout(repo: Path, cfg: SandboxConfig) -> AgentGitLayout:
    from myai.sandbox.provision import agent_staging_dir  # avoid import cycle

    digest = agent_staging_dir(repo).name  # same per-repo hash used elsewhere
    ws = effective_workspace_path(repo, cfg)
    return AgentGitLayout(
        host_git_dir=sandbox_agent_git_dir(digest),
        guest_git_dir=AGENT_GIT_MOUNT,
        guest_work_tree=ws,
        guest_objects=f"{ws}/.git/objects",
    )


def _real_objects_host_path(repo: Path) -> Path:
    return (repo / ".git" / "objects").resolve()


def seed(repo: Path, cfg: SandboxConfig) -> AgentGitLayout | None:
    """Create a fresh scratch git dir that shares the repo's objects.

    Returns the layout, or None when the mode is off or the repo is not a git
    repo (nothing to commit against, so plain read-only git is all the guest gets).
    """
    if not git_commit_mode(cfg) or not is_git_repo(repo):
        return None

    lay = layout(repo, cfg)
    scratch = lay.host_git_dir
    # Rebuilt every run so the agent starts from the repo's current state; its
    # previous work already lives in refs/sandbox/* on the real repo.
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.parent.mkdir(parents=True, exist_ok=True)

    _run_git(["clone", "--quiet", "--shared", "--bare", str((repo / ".git").resolve()), str(scratch)])
    # --shared wrote an alternate to the host objects path; point it at the
    # guest-visible path so the alternate resolves inside the VM. The host
    # rewrites it back before it reads the scratch (see import_refs).
    _write_alternate(scratch, lay.guest_objects)
    # Populate the index from HEAD so the mounted work tree reads as clean;
    # without this git treats every file as untracked and refuses to switch branches.
    _run_git(
        ["--git-dir", str(scratch), "--work-tree", str(repo.resolve()), "read-tree", "HEAD"],
    )
    return lay


def _write_alternate(scratch: Path, objects_path: str) -> None:
    info = scratch / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(objects_path + "\n", encoding="utf-8")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def import_refs(
    repo: Path, cfg: SandboxConfig, *, run_id: str | None = None
) -> list[tuple[str, str]]:
    """Import the agent's branches into refs/sandbox/<run>/* on the real repo.

    Returns (ref, short-sha) pairs. Objects are fsck'd on the way in, and the
    scratch alternate is reset to a trusted path first so a tampered pointer
    cannot send the read anywhere else.
    """
    if not git_commit_mode(cfg) or not is_git_repo(repo):
        return []
    lay = layout(repo, cfg)
    scratch = lay.host_git_dir
    if not scratch.is_dir():
        return []

    _write_alternate(scratch, str(_real_objects_host_path(repo)))
    run = run_id or _run_id()
    dest = f"{SANDBOX_REF_NAMESPACE}/{run}"

    # fsck every incoming object; never run a config/hook from the scratch repo.
    proc = subprocess.run(
        [
            "git",
            "-c", "transfer.fsckObjects=true",
            "-c", "fetch.fsckObjects=true",
            "-c", "core.hooksPath=/dev/null",
            "fetch",
            "--quiet",
            "--no-tags",
            str(scratch),
            f"+refs/heads/*:{dest}/*",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AgentGitError(f"importing agent commits failed: {detail}")

    listing = _run_git(
        ["for-each-ref", "--format=%(refname) %(objectname:short)", dest + "/"],
        cwd=repo,
    )
    out: list[tuple[str, str]] = []
    for line in listing.stdout.splitlines():
        ref, _, sha = line.partition(" ")
        if ref:
            out.append((ref, sha))
    return out
