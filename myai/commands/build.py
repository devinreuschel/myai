import argparse

from myai.commands import stub_not_implemented


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "build",
        help="Clone and build llama.cpp",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--version", metavar="TAG", help="Build a pinned release tag")
    group.add_argument("--latest", action="store_true", help="Build the latest release")
    group.add_argument("--head", action="store_true", help="Build from upstream HEAD")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return stub_not_implemented("build")
