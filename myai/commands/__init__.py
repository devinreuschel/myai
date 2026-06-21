import sys


def stub_not_implemented(command: str) -> int:
    print(f"myai {command}: not implemented yet", file=sys.stderr)
    return 1
