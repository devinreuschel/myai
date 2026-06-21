import argparse

from myai.commands import stub_not_implemented


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "check",
        help="Verify host prerequisites (git, cmake, compiler)",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return stub_not_implemented("check")
