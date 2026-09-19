import argparse
import sys
from pathlib import Path

from myai.sandbox.config import (
    SandboxConfig,
    SandboxConfigError,
    default_sandbox_config,
    load_config,
    repo_config_path,
    save_repo_config,
)
from myai.sandbox.doctor import doctor_ok, print_doctor, run_doctor as check_doctor
from myai.sandbox.gondolin import GondolinError, run_provision, run_sandbox
from myai.sandbox.session import SessionError
from myai.sandbox.trust import (
    UntrustedConfigError,
    describe_grants,
    is_trusted,
    load_trusted_config,
    load_unchecked_config,
    revoke_repo,
    trust_repo,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sandbox",
        help="Run pi inside a Gondolin micro-VM",
    )
    sandbox_sub = parser.add_subparsers(dest="sandbox_command", required=True)
    _register_run(sandbox_sub)
    _register_provision(sandbox_sub)
    _register_doctor(sandbox_sub)
    _register_init(sandbox_sub)
    _register_trust(sandbox_sub)
    parser.set_defaults(func=run)


def _register_run(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="Boot a sandboxed pi session")
    parser.add_argument("--path", default=".", help="Repo path (default: cwd)")
    parser.add_argument("--model-endpoint", help="Host model base URL (overrides config)")
    parser.add_argument("--allow-host", action="append", dest="allow_hosts", default=[])
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        default=[],
        help="Allow a known provider's domains (e.g. anthropic, openai); repeatable",
    )
    parser.add_argument(
        "--network",
        dest="network_policy",
        choices=["custom", "deny-all", "allow-all"],
        help="Network policy (default: custom; allow-all disables egress filtering)",
    )
    parser.add_argument("--vmm", choices=["auto", "qemu", "krun"], help="VM backend")
    parser.add_argument("--image", help="Gondolin image ref")
    parser.add_argument(
        "--rootfs-size",
        help="Guest root disk minimum size (e.g. 4G); requires e2fsprogs in the guest image",
    )
    parser.add_argument("--ro", action="store_true", help="Mount workspace read-only")
    parser.add_argument(
        "--hide",
        action="append",
        dest="guest_hidden_paths",
        default=[],
        help="Extra workspace path to hide from the guest (repeatable; /.myai is always hidden)",
    )
    git_group = parser.add_mutually_exclusive_group()
    git_group.add_argument(
        "--git-commit",
        action="store_true",
        help="Let the agent commit into a scratch clone; results import to refs/sandbox/* after the run",
    )
    git_group.add_argument(
        "--git-write",
        action="store_true",
        help="Let the guest write the real .git directly (hooks/config there run on the host; escape hatch)",
    )
    parser.add_argument(
        "--ignore-repo-config",
        action="store_true",
        help="Use only your global sandbox config; skip the repo's .myai/sandbox.json",
    )
    parser.add_argument(
        "--allow-remote-upstream",
        action="store_true",
        help="Allow host-loopback routes that point at other machines, not just this one",
    )
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
        "--debug",
        action="store_true",
        help="Report executables the guest tried to run but couldn't find",
    )
    parser.add_argument(
        "--no-auto-approve",
        action="store_true",
        help="Don't auto-approve pi tool calls (omit the injected -a flag)",
    )
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
    parser.add_argument(
        "--ignore-repo-config",
        action="store_true",
        help="Use only your global sandbox config; skip the repo's .myai/sandbox.json",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress messages")
    parser.set_defaults(func=run_provision_cmd)


def _register_doctor(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("doctor", help="Check sandbox prerequisites")
    parser.add_argument("--path", default=".", help="Repo path for config-aware checks (default: cwd)")
    parser.set_defaults(func=run_doctor)


def _register_init(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("init", help="Write default sandbox config for a repo")
    parser.add_argument("--path", default=".", help="Repo path (default: cwd)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing config")
    parser.set_defaults(func=run_init)


def _register_trust(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "trust",
        help="Review and approve a repo's .myai/sandbox.json (required before it is used)",
    )
    parser.add_argument("--path", default=".", help="Repo path (default: cwd)")
    parser.add_argument("-y", "--yes", action="store_true", help="Approve without prompting")
    parser.add_argument("--revoke", action="store_true", help="Withdraw approval")
    parser.set_defaults(func=run_trust)


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
            debug=args.debug,
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
        cfg = load_trusted_config(repo, ignore_repo_config=args.ignore_repo_config)
        _print_warnings(cfg)
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
    repo = Path(args.path).resolve()
    try:
        cfg = load_trusted_config(repo) if repo.is_dir() else SandboxConfig()
    except UntrustedConfigError:
        print(
            f"note: {repo_config_path(repo)} is not trusted yet; checking against your "
            "global config (run myai sandbox trust)",
            file=sys.stderr,
        )
        cfg = load_config(None)
    except SandboxConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    results = check_doctor(cfg)
    print_doctor(results)
    return 0 if doctor_ok(results) else 1


def run_init(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    path = repo_config_path(repo)
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force to replace it)", file=sys.stderr)
        return 1
    cfg = default_sandbox_config()
    try:
        save_repo_config(repo, cfg)
        # myai's own defaults, written at the user's request: nothing to review.
        trust_repo(repo)
        print(f"wrote {path}")
        print("edit it to allow hosts or secrets, then run myai sandbox trust to approve the change")
        return 0
    except SandboxConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def run_trust(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    path = repo_config_path(repo)
    if args.revoke:
        print(f"revoked trust for {path}" if revoke_repo(repo) else f"{path} was not trusted")
        return 0
    if not path.is_file():
        print(f"error: no sandbox config at {path}", file=sys.stderr)
        return 1
    try:
        cfg = load_unchecked_config(repo)
        if is_trusted(repo):
            print(f"{path} is already trusted")
            return 0
        print(f"{path}\nwith your global config, a sandbox run in this repo gets:\n")
        for line in describe_grants(cfg):
            print(f"  {line}")
        _print_warnings(cfg)
        print()
        if not args.yes:
            try:
                answer = input("Trust this config? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("not trusted")
                return 1
        trust_repo(repo)
        print("trusted; any change to the file will need approving again")
        return 0
    except SandboxConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _print_warnings(cfg: SandboxConfig) -> None:
    for warning in cfg.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _cfg_from_args(repo: Path, args: argparse.Namespace) -> SandboxConfig:
    cfg = load_trusted_config(
        repo, ignore_repo_config=getattr(args, "ignore_repo_config", False)
    )
    _print_warnings(cfg)
    if args.model_endpoint:
        cfg.model_endpoint = args.model_endpoint
        cfg.host_loopback.enabled = True
    if args.allow_hosts:
        cfg.allow_hosts = list(args.allow_hosts)
    if getattr(args, "providers", None):
        cfg.providers = list(args.providers)
    if getattr(args, "network_policy", None):
        cfg.network_policy = args.network_policy
    if getattr(args, "no_auto_approve", False):
        cfg.auto_approve = False
    if args.vmm:
        cfg.vmm = args.vmm
    if args.image:
        cfg.image = args.image
    if getattr(args, "rootfs_size", None):
        cfg.rootfs_size = args.rootfs_size
    if args.ro:
        cfg.mount_readonly = True
    if getattr(args, "guest_hidden_paths", None):
        # adds to the config's list; replacing it would drop paths hidden on purpose
        cfg.guest_hidden_paths = [
            *cfg.guest_hidden_paths,
            *(p for p in args.guest_hidden_paths if p not in cfg.guest_hidden_paths),
        ]
    if getattr(args, "git_commit", False):
        cfg.git_access = "commit"
    elif getattr(args, "git_write", False):
        cfg.git_access = "write"
    if getattr(args, "allow_remote_upstream", False):
        cfg.host_loopback.allow_remote_upstreams = True
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
