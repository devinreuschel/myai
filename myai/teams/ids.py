from __future__ import annotations

import re

_TASK_RE = re.compile(r"^[Tt]-(\d+)$")
# Doubles as the bot's home directory name, so keep it filesystem-safe.
_PRINCIPAL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class IdError(ValueError):
    pass


def parse_task_id(raw: str) -> int:
    match = _TASK_RE.match(raw.strip())
    if not match:
        raise IdError(f"expected task id like T-7, got {raw!r}")
    return int(match.group(1))


def format_task_id(task_id: int) -> str:
    return f"T-{task_id}"


def parse_principal_id(raw: str) -> str:
    value = raw.strip()
    if not _PRINCIPAL_RE.match(value):
        raise IdError(
            f"expected an id like dev-lead (lowercase letters, digits, hyphens; "
            f"max 32), got {raw!r}"
        )
    return value


def slugify(name: str) -> str:
    """Derive a principal id from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].rstrip("-")
    if not slug:
        raise IdError(f"cannot derive an id from {name!r}; pass one explicitly")
    return slug
