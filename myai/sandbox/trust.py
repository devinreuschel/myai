"""Approval gate for repo-level sandbox configs.

A repo's ``.myai/sandbox.json`` arrives with the clone, yet it decides what the
sandboxed agent may reach: which hosts, which host secrets, which host ports,
the ssh agent. So it stays inert until the user has approved that exact file,
and any edit to it needs a fresh look (the same deal as ``direnv allow``).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from myai.paths import sandbox_trust_path
from myai.sandbox.config import (
    DEFAULT_IMAGE,
    SandboxConfig,
    SandboxConfigError,
    _resolve_routes,
    effective_allow_hosts,
    effective_hidden_paths,
    load_config,
    repo_config_path,
    resolve_host_loopback_enabled,
)


class UntrustedConfigError(SandboxConfigError):
    pass


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _repo_key(repo: Path) -> str:
    return str(repo.resolve())


def _load_store() -> dict[str, str]:
    path = sandbox_trust_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # unreadable store trusts nothing
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _save_store(store: dict[str, str]) -> None:
    path = sandbox_trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(store, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _read_repo_config(repo: Path) -> bytes | None:
    path = repo_config_path(repo)
    if not path.is_file():
        return None
    return path.read_bytes()


def _parse(raw: bytes, repo: Path) -> dict:
    path = repo_config_path(repo)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SandboxConfigError(f"invalid sandbox config at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SandboxConfigError(f"invalid sandbox config at {path}: expected an object")
    return data


def is_trusted(repo: Path) -> bool:
    raw = _read_repo_config(repo)
    if raw is None:
        return True  # nothing to trust
    return _load_store().get(_repo_key(repo)) == _digest(raw)


def trust_repo(repo: Path) -> None:
    """Approve the repo's sandbox.json as it is on disk right now."""
    raw = _read_repo_config(repo)
    if raw is None:
        raise SandboxConfigError(f"no sandbox config at {repo_config_path(repo)}")
    store = _load_store()
    store[_repo_key(repo)] = _digest(raw)
    _save_store(store)


def revoke_repo(repo: Path) -> bool:
    store = _load_store()
    if store.pop(_repo_key(repo), None) is None:
        return False
    _save_store(store)
    return True


def load_unchecked_config(repo: Path) -> SandboxConfig:
    """Effective config including an unapproved repo file. For showing the user
    what they are about to approve; never for running anything."""
    raw = _read_repo_config(repo)
    if raw is None:
        return load_config(None)
    return load_config(repo, repo_data=_parse(raw, repo))


def load_trusted_config(repo: Path, *, ignore_repo_config: bool = False) -> SandboxConfig:
    """Config for a run: global, plus the repo's file only if it is approved."""
    raw = None if ignore_repo_config else _read_repo_config(repo)
    if raw is None:
        return load_config(None)
    # One read: the bytes that were approved are the bytes that get parsed.
    if _load_store().get(_repo_key(repo)) != _digest(raw):
        path = repo_config_path(repo)
        raise UntrustedConfigError(
            f"{path} is not trusted (new, or changed since you approved it).\n"
            "A repo's sandbox config decides what the sandboxed agent can reach, so it is\n"
            "ignored until you have looked at it:\n"
            f"  myai sandbox trust --path {repo}      review what it grants, then approve\n"
            "  myai sandbox run --ignore-repo-config    or run on your global config alone"
        )
    return load_config(repo, repo_data=_parse(raw, repo))


def describe_grants(cfg: SandboxConfig) -> list[str]:
    """What a config lets the guest do, in plain words, for the trust prompt."""
    lines: list[str] = []

    if cfg.network_policy == "allow-all":
        lines.append("network:    UNRESTRICTED egress (allow-all)")
    elif cfg.network_policy == "deny-all":
        lines.append("network:    none (deny-all)")
    else:
        hosts = effective_allow_hosts(cfg)
        lines.append(f"network:    {', '.join(hosts) if hosts else 'none (empty allow list)'}")

    for secret in cfg.host_secrets:
        source = secret.env_var or secret.name
        lines.append(
            f"secret:     host ${source} sent to {', '.join(secret.hosts)}"
            + (f" (as {secret.name})" if secret.env_var else "")
        )

    if resolve_host_loopback_enabled(cfg):
        for resolved in _resolve_routes(cfg):
            lines.append(
                f"host port:  guest {resolved.guest.guest_host}:{resolved.guest.port} "
                f"-> {resolved.upstream_host}:{resolved.upstream_port}"
            )

    if cfg.use_ssh_agent or cfg.ssh_allow_hosts:
        agent = "your ssh agent is forwarded" if cfg.use_ssh_agent else "no agent"
        lines.append(f"ssh:        {agent}; hosts: {', '.join(cfg.ssh_allow_hosts) or 'none'}")

    workspace = "read-only" if cfg.mount_readonly else "read-write"
    lines.append(f"workspace:  {workspace}; hidden: {', '.join(effective_hidden_paths(cfg))}")
    if cfg.git_access == "commit":
        lines.append("git:        agent commits to a scratch clone; imported to refs/sandbox/* after the run")
    elif cfg.git_access == "write":
        lines.append("git:        guest can WRITE the real .git (hooks and config there run on the host)")
    else:
        lines.append("git:        .git is read-only; the agent cannot commit")

    if cfg.share_host_sessions:
        lines.append("sessions:   this repo's pi sessions are shared with the guest")
    if cfg.mirror_host_pi:
        lines.append("host pi:    ~/.pi/agent/settings.json (packages, defaults) is mirrored in")
    if cfg.image != DEFAULT_IMAGE:
        lines.append(f"image:      {cfg.image}")
    lines.append(
        "approval:   pi auto-approves its own tool calls"
        if cfg.auto_approve
        else "approval:   pi asks before each tool call"
    )
    return lines
