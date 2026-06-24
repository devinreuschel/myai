"""Build JSON VM specs consumed by the Node Gondolin sidecar."""

from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from myai.sandbox.config import (
    DEFAULT_IMAGE,
    GUEST_AGENT_PATH,
    PI_INSTALL_MOUNT,
    SandboxConfig,
    effective_rootfs_size,
    effective_workspace_path,
    host_sessions_dir,
    provision_allow_hosts,
    resolve_host_loopback_enabled,
    resolve_host_loopback_routes,
    runtime_allow_host_args,
)
from myai.sandbox.provision import (
    build_pi_launch_shell,
    build_provision_shell,
    guest_agent_env,
    pi_bin_dir,
    pi_install_dir,
    pi_pkg_dir,
    prepare_agent_dir,
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
) -> dict[str, Any]:
    ws = effective_workspace_path(repo, cfg)
    mounts: list[dict[str, Any]] = [
        {
            "hostPath": str(staging.resolve()),
            "guestPath": GUEST_AGENT_PATH,
        },
    ]
    if cfg.share_host_sessions:
        sessions = host_sessions_dir()
        sessions.mkdir(parents=True, exist_ok=True)
        mounts.append({
            "hostPath": str(sessions.resolve()),
            "guestPath": f"{GUEST_AGENT_PATH}/sessions",
        })

    if cfg.install_pi_at_boot and cfg.image == DEFAULT_IMAGE:
        mounts.append({
            "hostPath": str(pi_install_dir().resolve()),
            "guestPath": PI_INSTALL_MOUNT,
        })
        mounts.append({
            "hostPath": str(pi_bin_dir().resolve()),
            "guestPath": f"{GUEST_AGENT_PATH}/bin",
        })
        if cfg.mirror_host_pi:
            for sub in ("npm", "git"):
                mounts.append({
                    "hostPath": str(pi_pkg_dir(sub).resolve()),
                    "guestPath": f"{GUEST_AGENT_PATH}/{sub}",
                })

    return {
        "workspace": {
            "hostPath": str(repo.resolve()),
            "guestPath": ws,
            "readonly": cfg.mount_readonly,
            "hiddenPaths": list(cfg.guest_hidden_paths),
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
    spec["env"] = _parse_env_lines(guest_agent_env(cfg, debug=debug))
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
        "vfs": _build_vfs_mounts(repo, cfg, staging),
    }
