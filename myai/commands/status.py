import argparse
import sys

from myai.agentsync.config import ConfigError, config_path, load_config, load_state
from myai.agentsync.registry import get_master, list_repos
from myai.agentsync.sync import compute_sync


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "status",
        help="Show master repo and managed repo sync status",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    master = get_master()
    if master is None:
        print("master: (not set)")
    else:
        exists = master.is_dir()
        suffix = "" if exists else " (missing)"
        print(f"master: {master}{suffix}")

    repos = list_repos()
    if not repos:
        print("repos: (none)")
        return 0

    print(f"repos: {len(repos)}")
    for repo in repos:
        cfg_path = config_path(repo)
        if not cfg_path.is_file():
            print(f"  {repo}: no config (stale registry entry?)")
            continue
        try:
            cfg = load_config(repo)
            old_state = load_state(repo)
            sync_plan = compute_sync(repo, cfg, old_state)
            n = len(sync_plan.actions)
            if n == 0:
                print(f"  {repo}: up to date")
            else:
                print(f"  {repo}: {n} change(s) pending")
                for action in sync_plan.actions:
                    detail = f" ({action.detail})" if action.detail else ""
                    print(f"    {action.kind}: {action.path}{detail}")
        except (ConfigError, Exception) as exc:
            print(f"  {repo}: error: {exc}", file=sys.stderr)
    return 0
