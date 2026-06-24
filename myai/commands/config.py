import argparse

from myai.global_config import get_inject_myai_rule_default, set_inject_myai_rule_default


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "config",
        help="Manage myai global configuration",
    )
    config_sub = parser.add_subparsers(dest="config_command", required=True)
    _register_myai_rule(config_sub)


def _register_myai_rule(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "myai-rule",
        help="Show or set the global default for injecting the myai-managed rule",
    )
    parser.add_argument(
        "value",
        nargs="?",
        choices=("on", "off"),
        help="Set global default (default: show current value)",
    )
    parser.set_defaults(func=run_myai_rule)


def run_myai_rule(args: argparse.Namespace) -> int:
    if args.value is None:
        enabled = get_inject_myai_rule_default()
        print("on" if enabled else "off")
        return 0
    enabled = args.value == "on"
    set_inject_myai_rule_default(enabled)
    print(f"inject_myai_rule default: {'on' if enabled else 'off'}")
    return 0
