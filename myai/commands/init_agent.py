import argparse
import sys
from pathlib import Path

from myai.agentsync.config import AGENTS, RepoConfig, config_path, save_config
from myai.agentsync.registry import add_repo, get_master


OVERWRITE_WARNING = """\
WARNING: this repo will be managed by myai.

Syncs overwrite agent config files that myai writes (.cursor/rules, .claude/rules,
.claude/skills, .pi/skills, managed blocks in CLAUDE.md/AGENTS.md, etc).

Edit rules and skills in the master repo, not in this repo.
Use --flat-rules to flatten rules into AGENTS.md/CLAUDE.md instead of nested files.
"""


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "init",
        help="Initialize myai agent config management in this repo",
    )
    parser.add_argument(
        "--agent",
        action="append",
        dest="agents",
        choices=AGENTS,
        help="Agent to manage (repeatable; default: all)",
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
        help="Flatten rules into AGENTS.md/CLAUDE.md instead of nested rule files",
    )
    parser.add_argument(
        "--no-myai-rule",
        action="store_true",
        help="Disable the myai-managed guardrail for this repo (overrides global default)",
    )
    parser.add_argument(
        "--path",
        default=".",
        help="Repo path (default: cwd)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip overwrite confirmation",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    master = get_master()
    if master is None:
        print("error: no master repo registered; run myai master init first", file=sys.stderr)
        return 1

    if config_path(repo).is_file():
        print(f"error: already initialized at {config_path(repo)}", file=sys.stderr)
        return 1

    if not args.yes:
        print(OVERWRITE_WARNING)
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("aborted")
            return 1

    agents = args.agents if args.agents else list(AGENTS)
    cfg = RepoConfig(
        agents=agents,
        rules=args.rules or [],
        skills=args.skills or [],
        subagents=args.subagents or [],
        nested_rules=not args.flat_rules,
        inject_myai_rule=False if args.no_myai_rule else None,
    )
    save_config(repo, cfg)
    add_repo(repo)

    print(f"initialized {repo}")
    print(f"master: {master}")
    print(f"config: {config_path(repo)}")
    print("run myai sync to apply rules and skills")
    return 0
