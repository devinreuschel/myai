import argparse

from myai import __version__
from myai.commands import build, check, init_agent, master, status, sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="myai")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check.register(subparsers)
    build.register(subparsers)
    init_agent.register(subparsers)
    master.register(subparsers)
    sync.register(subparsers)
    status.register(subparsers)

    args = parser.parse_args(argv)
    return args.func(args)
