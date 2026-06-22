import argparse
import sys
from pathlib import Path

from myai.agentsync.master import RULES_DIR, SKILLS_DIR, SUBAGENTS_DIR
from myai.agentsync.registry import set_master


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "master",
        help="Manage the central agent config repo",
    )
    master_sub = parser.add_subparsers(dest="master_command", required=True)
    _register_init(master_sub)
    parser.set_defaults(func=run)


def _register_init(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "init",
        help="Scaffold and register a master config repo",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to the master repo (default: cwd)",
    )
    parser.set_defaults(func=run_init)


def run(args: argparse.Namespace) -> int:
    return args.func(args)


def run_init(args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()
    path.mkdir(parents=True, exist_ok=True)

    for subdir in (RULES_DIR, SKILLS_DIR, SUBAGENTS_DIR):
        (path / subdir).mkdir(exist_ok=True)

    readme = path / "README.md"
    if not readme.exists():
        readme.write_text(
            "# AI config master repo\n\n"
            "Canonical rules, skills, and subagents synced to managed repos via `myai sync`.\n\n"
            f"- `{RULES_DIR}/` — rule files (`<name>.md` with optional frontmatter)\n"
            f"- `{SKILLS_DIR}/` — skill dirs (`<name>/SKILL.md`)\n"
            f"- `{SUBAGENTS_DIR}/` — subagent defs (claude only for now)\n",
            encoding="utf-8",
        )

    set_master(path)
    print(f"master repo initialized at {path}")
    print("add rules/skills/subagents, then run myai init in target repos")
    return 0
