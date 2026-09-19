"""Build JSON VM specs consumed by the Node Gondolin sidecar."""

from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from myai.sandbox.config import (
    DEFAULT_IMAGE,
    GIT_BUNDLE_MOUNT,
    GUEST_AGENT_PATH,
    PI_INSTALL_MOUNT,
    SandboxConfig,
    git_commit_mode,
    real_git_readonly,
    effective_hidden_paths,
    effective_rootfs_size,
    effective_workspace_path,
    provision_allow_hosts,
    resolve_host_loopback_enabled,
    resolve_host_loopback_routes,
    runtime_allow_host_args,
)
from myai.sandbox.agent_git import layout as agent_git_layout
from myai.sandbox.provision import (
    build_pi_launch_shell,
    build_provision_shell,
    guest_agent_env,
    pi_bin_dir,
    pi_install_dir,
    git_bundle_dir,
    pi_pkg_dir,
    prepare_agent_dir,
    session_slot_mount,
)


@dataclass
class VmSpecPlan:
    """Sidecar launch plan: JSON spec path payload and child env."""

    spec: dict[str, Any]
    mode: str  # run | provision


def _resolve_vmm(vmm: str) -> str | None:
    if vmm == "auto":
        if platform.machine().lower() in ("arm64", "aarch64") and platform.system() == "Darwin":
            if shutil.which("krun"):
                return "krun"
        return "qemu"
    if vmm in ("qemu", "krun"):
        return vmm
    return None


def _parse_env_lines(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        key, _, value = line.partition("=")
        if key:
            out[key] = value
    return out


def _build_network(cfg: SandboxConfig, *, provision: bool) -> dict[str, Any]:
    if provision:
        allow_hosts = provision_allow_hosts(cfg)
        unrestricted = False
    else:
        allow_hosts, unrestricted = runtime_allow_host_args(cfg)

    secrets: dict[str, dict[str, Any]] = {}
    for secret in cfg.host_secrets:
        secrets[secret.name] = {"hosts": list(secret.hosts)}

    tcp_hosts: dict[str, str] = {}
    if not provision and resolve_host_loopback_enabled(cfg):
        for resolved in resolve_host_loopback_routes(cfg):
            guest = resolved.guest
            key = f"{guest.guest_host}:{guest.port}"
            tcp_hosts[key] = f"{resolved.upstream_host}:{resolved.upstream_port}"

    if unrestricted:
        policy = "allow-all"
    elif cfg.network_policy == "deny-all":
        policy = "deny-all"
    else:
        policy = "custom"

    return {
        "policy": policy,
        "allowedHosts": allow_hosts,
        "secrets": secrets,
        "tcpHosts": tcp_hosts,
        "sshAllowHosts": list(cfg.ssh_allow_hosts),
        "useSshAgent": cfg.use_ssh_agent,
    }


def _build_vfs_mounts(
    repo: Path,
    cfg: SandboxConfig,
    staging: Path,
    *,
    provision: bool,
) -> dict[str, Any]:
    ws = effective_workspace_path(repo, cfg)
    mounts: list[dict[str, Any]] = [
        {
            "hostPath": str(staging.resolve()),
            "guestPath": GUEST_AGENT_PATH,
        },
    ]
    # The install VM has no use for transcripts.
    slot = None if provision else session_slot_mount(repo, cfg)
    if slot is not None:
        host_slot, guest_name = slot
        mounts.append({
            "hostPath": str(host_slot.resolve()),
            "guestPath": f"{GUEST_AGENT_PATH}/sessions/{guest_name}",
        })

    if cfg.install_pi_at_boot and cfg.image == DEFAULT_IMAGE:
        # One pi install serves every repo's sandbox, so only the install VM may
        # write it; a run that could would carry over into all later runs.
        cache_readonly = not provision
        cache_dirs = [pi_install_dir(), pi_bin_dir()]
        if cfg.mirror_host_pi:
            cache_dirs += [pi_pkg_dir(sub) for sub in ("npm", "git")]
        for cache_dir in cache_dirs:
            cache_dir.mkdir(parents=True, exist_ok=True)
        mounts.append({
            "hostPath": str(pi_install_dir().resolve()),
            "guestPath": PI_INSTALL_MOUNT,
            "readonly": cache_readonly,
        })
        mounts.append({
            "hostPath": str(pi_bin_dir().resolve()),
            "guestPath": f"{GUEST_AGENT_PATH}/bin",
            "readonly": cache_readonly,
        })
        bundle = git_bundle_dir()
        bundle.mkdir(parents=True, exist_ok=True)
        mounts.append({
            "hostPath": str(bundle.resolve()),
            "guestPath": GIT_BUNDLE_MOUNT,
            "readonly": cache_readonly,
        })
        if cfg.mirror_host_pi:
            for sub in ("npm", "git"):
                mounts.append({
                    "hostPath": str(pi_pkg_dir(sub).resolve()),
                    "guestPath": f"{GUEST_AGENT_PATH}/{sub}",
                    "readonly": cache_readonly,
                })

    if not provision and git_commit_mode(cfg) and (repo / ".git").is_dir():
        lay = agent_git_layout(repo, cfg)
        mounts.append({
            "hostPath": str(lay.host_git_dir.resolve()),
            "guestPath": lay.guest_git_dir,
        })

    return {
        "workspace": {
            "hostPath": str(repo.resolve()),
            "guestPath": ws,
            "readonly": cfg.mount_readonly,
            "hiddenPaths": effective_hidden_paths(cfg),
            # the real .git is never guest-writable except in the 'write' escape hatch
            "gitReadonly": real_git_readonly(cfg),
        },
        "mounts": mounts,
        "memfs": ["/tmp"],
    }


def build_run_spec(
    repo: Path,
    cfg: SandboxConfig,
    pi_args: list[str],
    *,
    debug: bool = False,
) -> VmSpecPlan:
    """Build a cold-boot VM spec that runs pi interactively."""
    staging = prepare_agent_dir(repo, cfg, debug=debug)
    ws = effective_workspace_path(repo, cfg)
    shell_cmd, shell_args = build_pi_launch_shell(cfg, pi_args, ws)
    spec = _base_spec(repo, cfg, staging, provision=False)
    spec["cwd"] = ws
    spec["env"] = _parse_env_lines(guest_agent_env(cfg, repo=repo, debug=debug))
    spec["command"] = [shell_cmd, *shell_args]
    spec["interactive"] = True
    spec["debug"] = debug
    return VmSpecPlan(spec=spec, mode="run")


def build_provision_spec(
    repo: Path,
    cfg: SandboxConfig,
) -> VmSpecPlan:
    """Build a one-shot VM spec for pi/npm provisioning."""
    staging = prepare_agent_dir(repo, cfg)
    ws = effective_workspace_path(repo, cfg)
    shell_cmd, shell_args = build_provision_shell(cfg)
    spec = _base_spec(repo, cfg, staging, provision=True)
    spec["cwd"] = ws
    spec["env"] = _parse_env_lines(guest_agent_env(cfg))
    spec["command"] = [shell_cmd, *shell_args]
    spec["interactive"] = False
    spec["debug"] = False
    return VmSpecPlan(spec=spec, mode="provision")


def _base_spec(
    repo: Path,
    cfg: SandboxConfig,
    staging: Path,
    *,
    provision: bool,
) -> dict[str, Any]:
    return {
        "mode": "provision" if provision else "run",
        "image": cfg.image or DEFAULT_IMAGE,
        "vmm": _resolve_vmm(cfg.vmm),
        "rootfsSize": effective_rootfs_size(cfg),
        "network": _build_network(cfg, provision=provision),
        "vfs": _build_vfs_mounts(repo, cfg, staging, provision=provision),
    }
