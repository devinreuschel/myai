import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from myai.paths import sandbox_locks_dir
from myai.sandbox.config import (
    SandboxConfig,
    effective_rootfs_size,
    sidecar_invocation,
)
from myai.sandbox.doctor import doctor_ok, failure_message, run_doctor
from myai.sandbox.provision import (
    agent_staging_dir,
    needs_provision,
    prepare_workspace_session_link,
    cleanup_workspace_session_link,
    read_debug_missing_exes,
)
from myai.sandbox.progress import RunProgress
from myai.sandbox.pty import run_foreground
from myai.sandbox.session import acquire_lock, release_lock
from myai.sandbox.sidecar_install import SidecarError, ensure_sidecar_installed
from myai.sandbox.vm_spec import VmSpecPlan, build_provision_spec, build_run_spec


class GondolinError(Exception):
    pass


@dataclass
class RunPlan:
    cmd: list[str]
    env: dict[str, str]
    spec_path: Path
    mode: str  # run | provision


def secret_child_env(cfg: SandboxConfig, base: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Resolve host_secrets into the child env and report any that are missing.

    The sidecar reads secret values from ``$NAME`` in its process env. When a
    secret declares ``env_var`` (a differently-named host variable) we copy that
    value into ``$NAME`` here, so the rename is honored without ever placing the
    value on the command line.
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


def _write_spec(plan: VmSpecPlan) -> Path:
    fd, path = tempfile.mkstemp(prefix="myai-vm-spec-", suffix=".json")
    os.close(fd)
    spec_path = Path(path)
    spec_path.write_text(json.dumps(plan.spec, indent=2) + "\n", encoding="utf-8")
    return spec_path


def build_run_plan(
    repo: Path,
    cfg: SandboxConfig,
    pi_args: list[str],
    *,
    progress: RunProgress | None = None,
    debug: bool = False,
) -> RunPlan:
    if progress:
        progress.say("Preparing guest agent config...")
    vm_plan = build_run_spec(repo, cfg, pi_args, debug=debug)

    if cfg.network_policy == "allow-all" and progress:
        progress.say("warning: network_policy is 'allow-all'; the sandbox has unrestricted network egress")

    spec_path = _write_spec(vm_plan)
    cmd = [*sidecar_invocation(cfg), str(spec_path)]
    env, missing = secret_child_env(cfg, os.environ.copy())
    _warn_missing_secrets(missing, progress)
    env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))
    return RunPlan(cmd=cmd, env=env, spec_path=spec_path, mode="run")


def build_provision_plan(
    repo: Path,
    cfg: SandboxConfig,
    *,
    progress: RunProgress | None = None,
) -> RunPlan:
    if progress:
        progress.say("Preparing guest agent config for provisioning...")
    vm_plan = build_provision_spec(repo, cfg)
    spec_path = _write_spec(vm_plan)
    cmd = [*sidecar_invocation(cfg), str(spec_path)]
    env, missing = secret_child_env(cfg, os.environ.copy())
    _warn_missing_secrets(missing, progress)
    env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))
    return RunPlan(cmd=cmd, env=env, spec_path=spec_path, mode="provision")


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
        results = run_doctor(cfg)
        if not doctor_ok(results):
            raise GondolinError(failure_message(results))
    progress.say("Installing Gondolin sidecar...")
    try:
        ensure_sidecar_installed(cfg, quiet=quiet)
    except SidecarError as exc:
        raise GondolinError(str(exc)) from exc
    progress.say("Provisioning pi (allows npm/github; not used at runtime)...")
    plan = build_provision_plan(repo, cfg, progress=progress)
    try:
        exit_code = run_foreground(plan.cmd, env=plan.env)
    finally:
        plan.spec_path.unlink(missing_ok=True)
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
        results = run_doctor(cfg)
        if not doctor_ok(results):
            raise GondolinError(failure_message(results))

    progress.say("Installing Gondolin sidecar...")
    try:
        ensure_sidecar_installed(cfg, quiet=quiet)
    except SidecarError as exc:
        raise GondolinError(str(exc)) from exc

    lock_path = _lock_path(repo)
    acquire_lock(lock_path)
    session_link = prepare_workspace_session_link(repo, cfg)
    plan: RunPlan | None = None
    try:
        if not skip_provision and needs_provision(cfg, force=reprovision):
            run_provision(repo, cfg, skip_doctor=True, quiet=quiet, force=reprovision)

        plan = build_run_plan(repo, cfg, pi_args, progress=progress, debug=debug)
        progress.say(f"Starting Gondolin VM ({cfg.image})...")
        if rootfs_size := effective_rootfs_size(cfg):
            progress.say(f"Guest root disk: {rootfs_size} minimum.")
        progress.say("First run may download guest assets (~100–200MB).")

        exit_code = run_foreground(plan.cmd, env=plan.env)
        if debug:
            _print_debug_audit(agent_staging_dir(repo))
        return exit_code
    finally:
        if plan is not None:
            plan.spec_path.unlink(missing_ok=True)
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


def _lock_path(repo: Path) -> Path:
    digest = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    return sandbox_locks_dir() / f"{digest}.lock"
