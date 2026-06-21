import argparse

from myai import __version__
from myai.commands import build, check


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

    args = parser.parse_args(argv)
    return args.func(args)
