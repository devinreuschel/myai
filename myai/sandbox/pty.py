import os
import signal
import subprocess
import sys
from collections.abc import Sequence


def run_foreground(cmd: Sequence[str], env: dict[str, str] | None = None) -> int:
    """Run a command with inherited stdio and signal forwarding."""
    if not sys.stdin.isatty():
        return subprocess.run(list(cmd), env=env, check=False).returncode

    proc = subprocess.Popen(
        list(cmd),
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=env,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    old_handlers: dict[int, object] = {}

    def relay(signum: int, _frame) -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, relay)

    if hasattr(signal, "SIGWINCH"):
        old_handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, relay)

    try:
        return proc.wait()
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
