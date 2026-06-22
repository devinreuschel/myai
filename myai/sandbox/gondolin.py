import hashlib
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from myai.paths import sandbox_locks_dir
from myai.sandbox.config import (
    DEFAULT_IMAGE,
    GUEST_AGENT_PATH,
    PI_INSTALL_MOUNT,
    SandboxConfig,
    effective_rootfs_size,
    effective_workspace_path,
    gondolin_invocation,
    host_sessions_dir,
    provision_allow_hosts,
    resolve_host_loopback_enabled,
    resolve_host_loopback_routes,
    runtime_allow_host_args,
)
from myai.sandbox.doctor import doctor_ok, failure_message, run_doctor
from myai.sandbox.provision import (
    agent_staging_dir,
    build_pi_launch_shell,
    build_provision_shell,
    cleanup_workspace_session_link,
    guest_agent_env,
    needs_provision,
    pi_bin_dir,
    pi_install_dir,
    pi_pkg_dir,
    prepare_agent_dir,
    prepare_workspace_session_link,
    read_debug_missing_exes,
)
from myai.sandbox.progress import RunProgress
from myai.sandbox.pty import run_foreground
from myai.sandbox.session import RepoSession, acquire_lock, list_repo_sessions, release_lock, save_session


class GondolinError(Exception):
    pass


@dataclass
class RunPlan:
    cmd: list[str]
    env: dict[str, str]
    mode: str  # boot | attach | resume


def secret_child_env(cfg: SandboxConfig, base: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Resolve host_secrets into the child env and report any that are missing.

    gondolin's ``--host-secret NAME@hosts`` reads the value from ``$NAME`` in the
    spawned process env. When a secret declares ``env_var`` (a differently-named
    host variable) we copy that value into ``$NAME`` here, so the rename is
    honored without ever placing the value on the command line. Returns the
    updated env and the list of secret names whose source variable was unset.
    """
    env = dict(base)
    missing: list[str] = []
    for secret in cfg.host_secrets:
        source = secret.env_var or secret.name
        value = base.get(source)
        if value is None:
            missing.append(secret.name)
            continue
        env[secret.name] = value
    return env, missing


def _warn_missing_secrets(missing: list[str], progress: RunProgress | None) -> None:
    for name in missing:
        msg = f"warning: secret {name!r} is configured but its host env var is unset; not injected"
        if progress:
            progress.say(msg)
        else:
            print(msg, file=sys.stderr)


def build_run_plan(
    repo: Path,
    cfg: SandboxConfig,
    pi_args: list[str],
    *,
    resume_id: str | None = None,
    session_id: str | None = None,
    progress: RunProgress | None = None,
    debug: bool = False,
) -> RunPlan:
    if progress:
        progress.say("Preparing guest agent config...")
    staging = prepare_agent_dir(repo, cfg, debug=debug)

    if cfg.network_policy == "allow-all" and progress:
        progress.say("warning: network_policy is 'allow-all'; the sandbox has unrestricted network egress")

    base = gondolin_invocation(cfg)
    cmd: list[str] = [*base]

    if session_id:
        cmd.extend(["attach", session_id])
    else:
        cmd.append("bash")
        if resume_id:
            cmd.extend(["--resume", resume_id])

    _append_common_flags(cmd, repo, cfg, staging, provision=False)

    ws = effective_workspace_path(repo, cfg)
    shell_cmd, shell_args = build_pi_launch_shell(cfg, pi_args, ws)
    cmd.extend(["--cwd", ws])
    for env_line in guest_agent_env(cfg, debug=debug):
        cmd.extend(["--env", env_line])
    cmd.append("--")
    cmd.append(shell_cmd)
    cmd.extend(shell_args)

    env, missing = secret_child_env(cfg, os.environ.copy())
    _warn_missing_secrets(missing, progress)
    env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))
    mode = "attach" if session_id else ("resume" if resume_id else "boot")
    return RunPlan(cmd=cmd, env=env, mode=mode)


def build_provision_plan(
    repo: Path,
    cfg: SandboxConfig,
    *,
    progress: RunProgress | None = None,
) -> RunPlan:
    if progress:
        progress.say("Preparing guest agent config for provisioning...")
    staging = prepare_agent_dir(repo, cfg)
    cmd: list[str] = [*gondolin_invocation(cfg), "bash"]
    _append_common_flags(cmd, repo, cfg, staging, provision=True)
    ws = effective_workspace_path(repo, cfg)
    shell_cmd, shell_args = build_provision_shell(cfg)
    cmd.extend(["--cwd", ws])
    for env_line in guest_agent_env(cfg):
        cmd.extend(["--env", env_line])
    cmd.append("--")
    cmd.append(shell_cmd)
    cmd.extend(shell_args)
    env, missing = secret_child_env(cfg, os.environ.copy())
    _warn_missing_secrets(missing, progress)
    env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))
    return RunPlan(cmd=cmd, env=env, mode="provision")


def _append_common_flags(
    cmd: list[str],
    repo: Path,
    cfg: SandboxConfig,
    staging: Path,
    *,
    provision: bool = False,
) -> None:
    ws = effective_workspace_path(repo, cfg)
    mount = f"{repo.resolve()}:{ws}"
    if cfg.mount_readonly:
        mount += ":ro"
    cmd.extend(["--mount-hostfs", mount])
    cmd.extend(["--mount-hostfs", f"{staging}:{GUEST_AGENT_PATH}"])
    if cfg.share_host_sessions:
        sessions = host_sessions_dir()
        sessions.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--mount-hostfs", f"{sessions.resolve()}:{GUEST_AGENT_PATH}/sessions"])
    cmd.extend(["--mount-memfs", "/tmp"])

    if cfg.install_pi_at_boot and cfg.image == DEFAULT_IMAGE:
        install_dir = pi_install_dir()
        install_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--mount-hostfs", f"{install_dir.resolve()}:{PI_INSTALL_MOUNT}"])
        bin_dir = pi_bin_dir()
        bin_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--mount-hostfs", f"{bin_dir.resolve()}:{GUEST_AGENT_PATH}/bin"])
        if cfg.mirror_host_pi:
            for sub in ("npm", "git"):
                pkg_dir = pi_pkg_dir(sub)
                pkg_dir.mkdir(parents=True, exist_ok=True)
                cmd.extend(["--mount-hostfs", f"{pkg_dir.resolve()}:{GUEST_AGENT_PATH}/{sub}"])

    if cfg.image:
        cmd.extend(["--image", cfg.image])

    if rootfs_size := effective_rootfs_size(cfg):
        cmd.extend(["--rootfs-size", rootfs_size])

    vmm = _resolve_vmm(cfg.vmm)
    if vmm:
        cmd.extend(["--vmm", vmm])

    if not provision and resolve_host_loopback_enabled(cfg):
        for resolved in resolve_host_loopback_routes(cfg):
            cmd.extend([
                "--tcp-map",
                f"{resolved.guest.guest_host}:{resolved.guest.port}="
                f"{resolved.upstream_host}:{resolved.upstream_port}",
            ])

    allow_hosts = provision_allow_hosts(cfg) if provision else runtime_allow_host_args(cfg)[0]
    for host in allow_hosts:
        cmd.extend(["--allow-host", host])

    for secret in cfg.host_secrets:
        hosts = ",".join(secret.hosts)
        # value comes from $name in the child env (secret_child_env), never argv
        spec = f"{secret.name}@{hosts}"
        cmd.extend(["--host-secret", spec])

    for ssh_host in cfg.ssh_allow_hosts:
        cmd.extend(["--ssh-allow-host", ssh_host])
    if cfg.use_ssh_agent:
        cmd.extend(["--ssh-agent"])


def _resolve_vmm(vmm: str) -> str | None:
    if vmm == "auto":
        if platform.machine().lower() in ("arm64", "aarch64") and platform.system() == "Darwin":
            if shutil.which("krun"):
                return "krun"
        return "qemu"
    if vmm in ("qemu", "krun"):
        return vmm
    return None


def run_provision(
    repo: Path,
    cfg: SandboxConfig,
    *,
    skip_doctor: bool = False,
    quiet: bool = False,
    force: bool = False,
) -> int:
    """Run the one-time provisioning VM (npm/github allowed)."""
    progress = RunProgress(quiet=quiet)
    if not needs_provision(cfg, force=force):
        progress.say("Sandbox already provisioned.")
        return 0
    if not skip_doctor:
        progress.say("Checking sandbox prerequisites...")
        results = run_doctor()
        if not doctor_ok(results):
            raise GondolinError(failure_message(results))
    progress.say("Provisioning pi (allows npm/github; not used at runtime)...")
    plan = build_provision_plan(repo, cfg, progress=progress)
    exit_code = run_foreground(plan.cmd, env=plan.env)
    if exit_code != 0:
        raise GondolinError(f"provisioning failed (exit {exit_code})")
    return exit_code


def run_sandbox(
    repo: Path,
    cfg: SandboxConfig,
    pi_args: list[str],
    *,
    skip_doctor: bool = False,
    skip_provision: bool = False,
    reprovision: bool = False,
    quiet: bool = False,
    debug: bool = False,
) -> int:
    progress = RunProgress(quiet=quiet)

    if not skip_doctor:
        progress.say("Checking sandbox prerequisites...")
        results = run_doctor()
        if not doctor_ok(results):
            raise GondolinError(failure_message(results))

    lock_path = _lock_path(repo)
    acquire_lock(lock_path)
    session_link = prepare_workspace_session_link(repo, cfg)
    try:
        session = _select_session(repo, cfg, progress=progress)
        attaching = session is not None and session.alive and session.session_id
        if not attaching and not skip_provision and needs_provision(cfg, force=reprovision):
            run_provision(repo, cfg, skip_doctor=True, quiet=quiet, force=reprovision)

        plan = build_run_plan(
            repo,
            cfg,
            pi_args,
            resume_id=session.resume_id if session else None,
            session_id=session.session_id if session and session.alive else None,
            progress=progress,
            debug=debug,
        )
        if plan.mode == "attach":
            progress.say(f"Attaching to warm session {session.session_id}...")
        elif plan.mode == "resume":
            progress.say(f"Resuming from checkpoint {session.resume_id}...")
        else:
            progress.say(f"Starting Gondolin VM ({cfg.image})...")
            if rootfs_size := effective_rootfs_size(cfg):
                progress.say(f"Guest root disk: {rootfs_size} minimum.")
            progress.say("First run may download guest assets (~100–200MB).")

        exit_code = run_foreground(plan.cmd, env=plan.env)
        if debug:
            _print_debug_audit(agent_staging_dir(repo))
        return exit_code
    finally:
        cleanup_workspace_session_link(session_link)
        release_lock(lock_path)


def _print_debug_audit(staging: Path) -> None:
    """Print executables the guest tried to run but could not find."""
    missing = read_debug_missing_exes(staging)
    if not missing:
        return
    print("\n[sandbox debug audit]", file=sys.stderr)
    print("  missing executables (attempted but not found in guest):", file=sys.stderr)
    for exe in missing:
        print(f"    {exe}", file=sys.stderr)


def _select_session(
    repo: Path,
    cfg: SandboxConfig,
    *,
    progress: RunProgress | None = None,
) -> RepoSession | None:
    if not cfg.warm_reuse:
        return None
    if progress:
        progress.say("Checking for warm sessions...")
    sessions = list_repo_sessions()
    live = _gondolin_list(progress=progress)
    for session in sessions:
        if session.repo_path != str(repo.resolve()):
            continue
        if session.session_id and session.session_id in live:
            session.alive = True
            return session
        if session.resume_id:
            session.alive = False
            return session
    return None


def _gondolin_list(*, progress: RunProgress | None = None) -> dict[str, bool]:
    cfg = SandboxConfig()
    cmd = [*gondolin_invocation(cfg), "list"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except Exception:
        return {}
    live: dict[str, bool] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("ID"):
            continue
        parts = line.split()
        if parts:
            live[parts[0]] = True
    return live


def _lock_path(repo: Path) -> Path:
    digest = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    return sandbox_locks_dir() / f"{digest}.lock"


def gondolin_list_output() -> str:
    cmd = [*gondolin_invocation(SandboxConfig()), "list", "--all"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        return out.stdout or out.stderr
    except Exception as exc:
        return str(exc)


def gondolin_snapshot(session_id: str, name: str | None = None) -> str:
    cmd = [*gondolin_invocation(SandboxConfig()), "snapshot", session_id]
    if name:
        cmd.extend(["--name", name])
    out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
    if out.returncode != 0:
        raise GondolinError(out.stderr or out.stdout or f"snapshot failed ({out.returncode})")
    return out.stdout.strip()


def register_session(
    repo: Path,
    session_id: str | None,
    resume_id: str | None = None,
    *,
    alive: bool = True,
) -> None:
    """Store a session for warm reuse. A resume-only entry (no live VM) is not alive."""
    save_session(
        RepoSession(
            repo_path=str(repo.resolve()),
            session_id=session_id,
            resume_id=resume_id,
            alive=alive,
        )
    )
