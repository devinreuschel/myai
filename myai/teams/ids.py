from __future__ import annotations

import re

_EPIC_RE = re.compile(r"^[Ee]-(\d+)$")
_TASK_RE = re.compile(r"^[Tt]-(\d+)$")


class IdError(ValueError):
    pass


def parse_epic_id(raw: str) -> int:
    match = _EPIC_RE.match(raw.strip())
    if not match:
        raise IdError(f"expected epic id like E-12, got {raw!r}")
    return int(match.group(1))


def parse_task_id(raw: str) -> int:
    match = _TASK_RE.match(raw.strip())
    if not match:
        raise IdError(f"expected task id like T-7, got {raw!r}")
    return int(match.group(1))


def format_epic_id(epic_id: int) -> str:
    return f"E-{epic_id}"


def format_task_id(task_id: int) -> str:
    return f"T-{task_id}"
