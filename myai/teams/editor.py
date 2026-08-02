from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path


class EditorError(RuntimeError):
    pass


class EditRejected(EditorError):
    """parse() rejected the edited text; the file is kept at `path`."""

    def __init__(self, cause: Exception, path: Path) -> None:
        super().__init__(f"{cause}; edit kept at {path}")
        self.cause = cause
        self.path = path


def _identity(text: str) -> str:
    return text


def edit_text[T](
    initial: str,
    *,
    suffix: str = ".yaml",
    parse: Callable[[str], T] = _identity,
) -> T:
    """Open $EDITOR/$VISUAL on a tempfile seeded with initial; return parse(text).

    The tempfile is removed once parse accepts the edit. If the editor fails or
    parse rejects the content, the file is kept and its path is named in the
    error, so a long edit is never lost to a typo.

    Raises EditorError if no editor is set, it cannot be run, or it exits
    non-zero; EditRejected if parse raises.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise EditorError("set $EDITOR or $VISUAL to edit")
    # $EDITOR routinely carries flags ("code -w", "emacsclient -nw").
    argv = shlex.split(editor)
    if not argv:
        raise EditorError("$EDITOR is empty")

    fd, path_str = tempfile.mkstemp(prefix="myai-teams-", suffix=suffix)
    path = Path(path_str)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(initial)

    try:
        result = subprocess.run([*argv, str(path)], check=False)
    except OSError as exc:
        raise EditorError(f"could not run editor {editor!r}: {exc}") from exc
    if result.returncode != 0:
        raise EditorError(
            f"editor exited with {result.returncode}; edit kept at {path}"
        )

    try:
        parsed = parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise EditRejected(exc, path) from exc

    path.unlink(missing_ok=True)
    return parsed
