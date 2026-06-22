import json
from pathlib import Path

from myai.paths import agentsync_registry_path


class RegistryError(Exception):
    pass


def load() -> dict:
    path = agentsync_registry_path()
    if not path.is_file():
        return {"master": None, "repos": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"invalid registry at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"invalid registry at {path}: expected object")
    data.setdefault("master", None)
    data.setdefault("repos", [])
    return data


def save(data: dict) -> None:
    path = agentsync_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def set_master(path: Path) -> None:
    data = load()
    data["master"] = str(path.resolve())
    save(data)


def get_master() -> Path | None:
    data = load()
    master = data.get("master")
    if not master:
        return None
    return Path(master).resolve()


def add_repo(path: Path) -> None:
    resolved = str(path.resolve())
    data = load()
    repos = data.setdefault("repos", [])
    if resolved not in repos:
        repos.append(resolved)
        save(data)


def remove_repo(path: Path) -> None:
    resolved = str(path.resolve())
    data = load()
    repos = data.setdefault("repos", [])
    if resolved in repos:
        repos.remove(resolved)
        save(data)


def list_repos() -> list[Path]:
    data = load()
    return [Path(p).resolve() for p in data.get("repos", [])]
