import argparse
import re
import sys
from pathlib import Path

from myai.sandbox.config import (
    SandboxConfig,
    SandboxConfigError,
    default_sandbox_config,
    load_config,
    save_repo_config,
)
from myai.sandbox.doctor import doctor_ok, print_doctor, run_doctor as check_doctor
from myai.sandbox.gondolin import (
    GondolinError,
    gondolin_list_output,
    gondolin_snapshot,
    register_session,
    run_provision,
    run_sandbox,
)
from myai.sandbox.session import SessionError, load_sessions, remove_session


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sandbox",
        help="Run pi inside a Gondolin micro-VM",
    )
    sandbox_sub = parser.add_subparsers(dest="sandbox_command", required=True)
    _register_run(sandbox_sub)
    _register_provision(sandbox_sub)
    _register_doctor(sandbox_sub)
    _register_ls(sandbox_sub)
    _register_stop(sandbox_sub)
    _register_snapshot(sandbox_sub)
    _register_init(sandbox_sub)
    _register_register(sandbox_sub)
    parser.set_defaults(func=run)


def _register_run(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="Boot or attach a sandboxed pi session")
    parser.add_argument("--path", default=".", help="Repo path (default: cwd)")
    parser.add_argument("--model-endpoint", help="Host model base URL (overrides config)")
    parser.add_argument("--allow-host", action="append", dest="allow_hosts", default=[])
    parser.add_argument("--vmm", choices=["auto", "qemu", "krun"], help="VM backend")
    parser.add_argument("--image", help="Gondolin image ref")
    parser.add_argument(
        "--rootfs-size",
        help="Guest root disk minimum size (e.g. 4G); requires e2fsprogs in the guest image",
    )
    parser.add_argument("--no-warm", action="store_true", help="Disable warm VM reuse")
    parser.add_argument("--ro", action="store_true", help="Mount workspace read-only")
    parser.add_argument("--skip-doctor", action="store_true", help="Skip prerequisite checks")
    parser.add_argument(
        "--skip-provision",
        action="store_true",
        help="Skip one-time pi provisioning (fails if pi/tools not cached)",
    )
    parser.add_argument(
        "--reprovision",
        action="store_true",
        help="Re-run pi provisioning even if already cached",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress messages")
    parser.add_argument(
        "--mirror-host-pi",
        action="store_true",
        help="Mirror host ~/.pi/agent settings (packages, default provider/model) into the VM",
    )
    loopback = parser.add_mutually_exclusive_group()
    loopback.add_argument("--host-loopback", action="store_true", help="Enable host loopback for this run")
    loopback.add_argument("--no-host-loopback", action="store_true", help="Disable host loopback for this run")
    parser.add_argument(
        "pi_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to pi (prefix with --)",
    )
    parser.set_defaults(func=run_run)


def _register_provision(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "provision",
        help="Install pi and pre-fetch tools (allows npm/github; one-time)",
    )
    parser.add_argument("--path", default=".", help="Repo path (default: cwd)")
    parser.add_argument("--skip-doctor", action="store_true", help="Skip prerequisite checks")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run provisioning even if already cached",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress messages")
    parser.set_defaults(func=run_provision_cmd)


def _register_doctor(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("doctor", help="Check sandbox prerequisites")
    parser.set_defaults(func=run_doctor)


def _register_ls(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("ls", help="List warm sessions and live gondolin VMs")
    parser.set_defaults(func=run_ls)


def _register_stop(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("stop", help="Clear myai session registry for repos")
    parser.add_argument("--all", action="store_true", help="Clear all registered sessions")
    parser.add_argument("--repo", help="Clear session for a specific repo path")
    parser.set_defaults(func=run_stop)


def _register_snapshot(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("snapshot", help="Snapshot a live gondolin session")
    parser.add_argument("session_id", help="Gondolin session UUID or prefix")
    parser.add_argument("--name", help="Checkpoint name")
    parser.add_argument("--repo", help="Associate snapshot with repo for warm resume")
    parser.set_defaults(func=run_snapshot)


def _register_register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("register", help="Register a live gondolin session for warm reuse")
    parser.add_argument("session_id", help="Gondolin session UUID or prefix")
    parser.add_argument("--repo", default=".", help="Repo path (default: cwd)")
    parser.set_defaults(func=run_register)


def _register_init(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("init", help="Write default sandbox config for a repo")
    parser.add_argument("--path", default=".", help="Repo path (default: cwd)")
    parser.set_defaults(func=run_init)


def run(args: argparse.Namespace) -> int:
    return args.func(args)


def run_run(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 1
    try:
        cfg = _cfg_from_args(repo, args)
        pi_args = _normalize_pi_args(args.pi_args)
        return run_sandbox(
            repo,
            cfg,
            pi_args,
            skip_doctor=args.skip_doctor,
            skip_provision=args.skip_provision,
            reprovision=args.reprovision,
            quiet=args.quiet,
        )
    except (SandboxConfigError, GondolinError, SessionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def run_provision_cmd(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 1
    try:
        cfg = load_config(repo)
        cfg.validate()
        return run_provision(
            repo,
            cfg,
            skip_doctor=args.skip_doctor,
            quiet=args.quiet,
            force=args.force,
        )
    except (SandboxConfigError, GondolinError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def run_doctor(args: argparse.Namespace) -> int:
    results = check_doctor()
    print_doctor(results)
    return 0 if doctor_ok(results) else 1


def run_ls(args: argparse.Namespace) -> int:
    sessions = load_sessions()
    if sessions:
        print("myai sessions:")
        for session in sessions:
            state = "alive" if session.alive else "stored"
            sid = session.session_id or "-"
            rid = session.resume_id or "-"
            print(f"  {session.repo_path}: session={sid} resume={rid} ({state})")
    else:
        print("myai sessions: (none)")

    print("\ngondolin sessions:")
    print(gondolin_list_output().rstrip() or "(none)")
    return 0


def run_stop(args: argparse.Namespace) -> int:
    if args.all:
        for session in load_sessions():
            remove_session(session.repo_path)
        print("cleared all myai sandbox session entries")
        return 0
    if args.repo:
        remove_session(str(Path(args.repo).resolve()))
        print(f"cleared session for {args.repo}")
        return 0
    print("error: pass --all or --repo", file=sys.stderr)
    return 1


def run_snapshot(args: argparse.Namespace) -> int:
    try:
        output = gondolin_snapshot(args.session_id, name=args.name)
        print(output)
        if args.repo:
            repo = Path(args.repo).resolve()
            resume_id = _parse_resume_id(output) or args.session_id
            register_session(repo, session_id=None, resume_id=resume_id)
            print(f"registered resume {resume_id} for {repo}")
        return 0
    except GondolinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parse_resume_id(output: str) -> str | None:
    match = re.search(r"--resume\s+(\S+)", output)
    return match.group(1) if match else None


def run_register(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    register_session(repo, session_id=args.session_id)
    print(f"registered session {args.session_id} for {repo}")
    return 0


def run_init(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    cfg = default_sandbox_config()
    try:
        save_repo_config(repo, cfg)
        print(f"wrote {repo / '.myai' / 'sandbox.json'}")
        return 0
    except SandboxConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cfg_from_args(repo: Path, args: argparse.Namespace) -> SandboxConfig:
    cfg = load_config(repo)
    if args.model_endpoint:
        cfg.model_endpoint = args.model_endpoint
        cfg.host_loopback.enabled = True
    if args.allow_hosts:
        cfg.allow_hosts = list(args.allow_hosts)
    if args.vmm:
        cfg.vmm = args.vmm
    if args.image:
        cfg.image = args.image
    if getattr(args, "rootfs_size", None):
        cfg.rootfs_size = args.rootfs_size
    if args.no_warm:
        cfg.warm_reuse = False
    if args.ro:
        cfg.mount_readonly = True
    if args.host_loopback:
        cfg.host_loopback.enabled = True
    if args.no_host_loopback:
        cfg.host_loopback.enabled = False
    if getattr(args, "mirror_host_pi", False):
        cfg.mirror_host_pi = True
    cfg.validate()
    return cfg


def _normalize_pi_args(pi_args: list[str]) -> list[str]:
    if pi_args and pi_args[0] == "--":
        return pi_args[1:]
    return pi_args
