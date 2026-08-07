import argparse
import sys

from myai.agentsync.config import AGENTS
from myai.agentsync.global_config import (
    GlobalSyncConfig,
    config_exists,
    load_global_sync_config,
    load_global_sync_state,
    save_global_sync_config,
)
from myai.agentsync.global_homes import agent_home
from myai.agentsync.global_sync import (
    compute_global_sync,
    format_conflict_paths,
    print_global_sync_result,
    sync_global,
)
from myai.agentsync.registry import get_master
from myai.paths import global_sync_config_path


OVERWRITE_WARNING = """\
WARNING: myai will manage selected agent home directories.

Syncs overwrite files myai writes under ~/.claude, ~/.cursor, and ~/.pi/agent
(skills, rules, managed blocks in CLAUDE.md/AGENTS.md, etc).

Edit rules and skills in the master repo, not in agent homes.
Cursor has no file-backed global rules; only skills sync for cursor.
"""

CLOBBER_WARNING = """\
WARNING: the following paths already exist and are not tracked by myai.
Syncing will overwrite them (skill dirs are replaced entirely):
"""


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "global",
        help="Sync rules/skills into user-global agent homes",
    )
    global_sub = parser.add_subparsers(dest="global_command", required=True)
    _register_init(global_sub)
    _register_sync(global_sub)
    _register_status(global_sub)


def _register_init(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "init",
        help="Configure which agents/artifacts sync to user-global homes",
    )
    parser.add_argument(
        "--agent",
        action="append",
        dest="agents",
        choices=AGENTS,
        help="Agent home to manage (repeatable; default: all)",
    )
    parser.add_argument(
        "--rule",
        action="append",
        dest="rules",
        default=[],
        help="Rule name from master (repeatable)",
    )
    parser.add_argument(
        "--skill",
        action="append",
        dest="skills",
        default=[],
        help="Skill name from master (repeatable)",
    )
    parser.add_argument(
        "--subagent",
        action="append",
        dest="subagents",
        default=[],
        help="Subagent name from master (repeatable)",
    )
    parser.add_argument(
        "--flat-rules",
        action="store_true",
        help="Flatten rules into CLAUDE.md/AGENTS.md instead of nested rule files",
    )
    parser.add_argument(
        "--no-myai-rule",
        action="store_true",
        help="Disable the myai-managed guardrail for global homes",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip overwrite confirmation",
    )
    parser.set_defaults(func=run_init)


def _register_sync(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sync",
        help="Apply global selection to agent home directories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without writing",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Overwrite untracked conflicting files without prompting",
    )
    parser.set_defaults(func=run_sync)


def _register_status(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "status",
        help="Show global sync selection and pending changes",
    )
    parser.set_defaults(func=run_status)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


def run_init(args: argparse.Namespace) -> int:
    master = get_master()
    if master is None:
        print("error: no master repo registered; run myai master init first", file=sys.stderr)
        return 1

    replacing = config_exists()
    if not args.yes:
        print(OVERWRITE_WARNING)
        if replacing:
            print(f"Existing config at {global_sync_config_path()} will be replaced.\n")
        if not _confirm("Continue? [y/N] "):
            print("aborted")
            return 1

    agents = args.agents if args.agents else list(AGENTS)
    cfg = GlobalSyncConfig(
        agents=agents,
        rules=args.rules or [],
        skills=args.skills or [],
        subagents=args.subagents or [],
        nested_rules=not args.flat_rules,
        inject_myai_rule=False if args.no_myai_rule else None,
    )
    save_global_sync_config(cfg)

    print(f"{'updated' if replacing else 'initialized'} {global_sync_config_path()}")
    print(f"master: {master}")
    for agent in agents:
        print(f"  {agent}: {agent_home(agent)}")
    if "cursor" in agents and cfg.rules:
        print(
            "note: cursor global rules are unsupported; "
            "rules sync to claude/pi only",
            file=sys.stderr,
        )
    print("run myai global sync to apply rules and skills")
    return 0


def run_sync(args: argparse.Namespace) -> int:
    allow_clobber = bool(args.yes)
    if not args.dry_run and not allow_clobber:
        try:
            cfg = load_global_sync_config()
            old_state = load_global_sync_state()
            plan = compute_global_sync(cfg, old_state)
        except Exception as exc:
            print(f"global: error: {exc}", file=sys.stderr)
            return 1
        if plan.conflicts:
            print(CLOBBER_WARNING, file=sys.stderr)
            for path in format_conflict_paths(plan.conflicts):
                print(f"  {path}", file=sys.stderr)
            print(
                "\nBack up or move these paths first, or confirm overwrite.",
                file=sys.stderr,
            )
            if not _confirm("Overwrite untracked files? [y/N] "):
                print("aborted")
                return 1
            allow_clobber = True

    result = sync_global(dry_run=args.dry_run, allow_clobber=allow_clobber)
    print_global_sync_result(result, args.dry_run)
    return 1 if result.error else 0


def run_status(args: argparse.Namespace) -> int:
    master = get_master()
    if master is None:
        print("master: (not set)")
    else:
        exists = master.is_dir()
        suffix = "" if exists else " (missing)"
        print(f"master: {master}{suffix}")

    if not config_exists():
        print("global: (not configured; run myai global init)")
        return 0

    try:
        cfg = load_global_sync_config()
        print(f"config: {global_sync_config_path()}")
        print(f"agents: {', '.join(cfg.agents)}")
        print(f"rules: {', '.join(cfg.rules) if cfg.rules else '(none)'}")
        print(f"skills: {', '.join(cfg.skills) if cfg.skills else '(none)'}")
        print(f"subagents: {', '.join(cfg.subagents) if cfg.subagents else '(none)'}")
        for agent in cfg.agents:
            print(f"  {agent} home: {agent_home(agent)}")

        old_state = load_global_sync_state()
        sync_plan = compute_global_sync(cfg, old_state)
        for warning in sync_plan.warnings:
            print(warning, file=sys.stderr)
        if sync_plan.conflicts:
            print("conflicts (would overwrite untracked files):", file=sys.stderr)
            for path in format_conflict_paths(sync_plan.conflicts):
                print(f"  {path}", file=sys.stderr)
            print("resolve or re-run sync with -y after confirming", file=sys.stderr)
        n = len(sync_plan.actions)
        if n == 0:
            print("status: up to date")
        else:
            print(f"status: {n} change(s) pending")
            for action in sync_plan.actions:
                detail = f" ({action.detail})" if action.detail else ""
                print(f"  {action.kind}: {action.path}{detail}")
    except Exception as exc:
        print(f"global: error: {exc}", file=sys.stderr)
        return 1
    return 0
