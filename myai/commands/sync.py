import argparse
import sys
from pathlib import Path

from myai.agentsync.registry import get_master, list_repos
from myai.agentsync.sync import print_sync_result, sync_all, sync_repo


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sync",
        help="Sync agent rules/skills from master to managed repos",
    )
    parser.add_argument(
        "--repo",
        help="Sync a single repo (default: all registered repos)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without writing",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    master = get_master()
    if master is None:
        print("error: no master repo registered; run myai master init first", file=sys.stderr)
        return 1
    if not master.is_dir():
        print(f"error: master repo not found at {master}", file=sys.stderr)
        return 1

    if args.repo:
        repos = [Path(args.repo).resolve()]
    else:
        repos = list_repos()
        if not repos:
            print("no managed repos; run myai init in target repos", file=sys.stderr)
            return 1

    results = sync_all(repos, dry_run=args.dry_run)
    exit_code = 0
    for result in results:
        print_sync_result(result, args.dry_run)
        if result.error:
            exit_code = 1
    return exit_code
