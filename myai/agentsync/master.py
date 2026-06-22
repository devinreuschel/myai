import re
from dataclasses import dataclass, field
from pathlib import Path

RULES_DIR = "rules"
SKILLS_DIR = "skills"
SUBAGENTS_DIR = "subagents"


class MasterError(Exception):
    pass


@dataclass
class Frontmatter:
    raw: dict[str, str | list[str] | bool] = field(default_factory=dict)

    def get_str(self, key: str, default: str = "") -> str:
        val = self.raw.get(key, default)
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        if isinstance(val, bool):
            return str(val).lower()
        return str(val) if val is not None else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.raw.get(key)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "yes", "1")
        return default

    def get_list(self, key: str) -> list[str]:
        val = self.raw.get(key)
        if val is None:
            return []
        if isinstance(val, list):
            return [str(v) for v in val]
        if isinstance(val, str):
            return [v.strip() for v in val.split(",") if v.strip()]
        return []


@dataclass
class Rule:
    name: str
    path: Path
    frontmatter: Frontmatter
    body: str


@dataclass
class Skill:
    name: str
    path: Path


@dataclass
class Subagent:
    name: str
    path: Path
    frontmatter: Frontmatter
    body: str


def parse_frontmatter(text: str) -> tuple[Frontmatter, str]:
    if not text.startswith("---"):
        return Frontmatter(), text
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.DOTALL)
    if not match:
        return Frontmatter(), text
    fm_text = match.group(1)
    body = text[match.end() :]
    return Frontmatter(raw=_parse_yaml_subset(fm_text)), body


def _parse_yaml_subset(text: str) -> dict[str, str | list[str] | bool]:
    """Minimal frontmatter parser: key: value, lists with - items."""
    result: dict[str, str | list[str] | bool] = {}
    current_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_key:
            item = stripped[2:].strip().strip("'\"")
            existing = result.get(current_key)
            if isinstance(existing, list):
                existing.append(item)
            else:
                result[current_key] = [item]
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        current_key = key
        if not val:
            result[key] = []
            continue
        if val.lower() in ("true", "false"):
            result[key] = val.lower() == "true"
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            result[key] = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
        else:
            result[key] = val.strip("'\"")
    return result


def _rule_applies_to_agent(fm: Frontmatter, agent: str) -> bool:
    agents = fm.get_list("agents")
    if not agents:
        return True
    return agent in agents


def _normalize_selector(selector: str) -> str:
    sel = selector.strip().strip("/")
    if sel.endswith(".md"):
        sel = sel[:-3]
    if not sel or sel.startswith("/") or ".." in Path(sel).parts:
        raise MasterError(f"invalid rule selector {selector!r}")
    return sel


def _rules_dir(master: Path) -> Path:
    return master / RULES_DIR


def _read_rule(path: Path, name: str) -> Rule:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    return Rule(name=name, path=path, frontmatter=fm, body=body.strip())


def resolve_rule_selector(master: Path, selector: str) -> list[Rule]:
    sel = _normalize_selector(selector)
    rules_dir = _rules_dir(master)
    rules: list[Rule] = []

    file_path = rules_dir / f"{sel}.md"
    if file_path.is_file():
        rules.append(_read_rule(file_path, sel))

    dir_path = rules_dir / sel
    if dir_path.is_dir():
        for md in sorted(dir_path.glob("*.md")):
            if md.is_file():
                rules.append(_read_rule(md, f"{sel}/{md.stem}"))

    if not rules:
        raise MasterError(f"rule selector {selector!r} matched nothing")
    return rules


def list_rules(master: Path) -> list[str]:
    rules_dir = _rules_dir(master)
    if not rules_dir.is_dir():
        return []
    names: set[str] = set()
    for p in sorted(rules_dir.glob("*.md")):
        names.add(p.stem)
    for p in sorted(rules_dir.iterdir()):
        if p.is_dir():
            names.add(p.name)
    return sorted(names)


def list_skills(master: Path) -> list[str]:
    skills_dir = master / SKILLS_DIR
    if not skills_dir.is_dir():
        return []
    names = []
    for p in sorted(skills_dir.iterdir()):
        if p.is_dir() and (p / "SKILL.md").is_file():
            names.append(p.name)
    return names


def list_subagents(master: Path) -> list[str]:
    sub_dir = master / SUBAGENTS_DIR
    if not sub_dir.is_dir():
        return []
    return sorted(p.stem for p in sub_dir.glob("*.md"))


def load_skill(master: Path, name: str) -> Skill:
    path = master / SKILLS_DIR / name
    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        raise MasterError(f"skill {name!r} not found at {skill_md}")
    return Skill(name=name, path=path)


def load_subagent(master: Path, name: str) -> Subagent:
    path = master / SUBAGENTS_DIR / f"{name}.md"
    if not path.is_file():
        raise MasterError(f"subagent {name!r} not found at {path}")
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    return Subagent(name=name, path=path, frontmatter=fm, body=body.strip())


def resolve_selection(
    master: Path,
    rule_names: list[str],
    skill_names: list[str],
    subagent_names: list[str],
) -> tuple[list[Rule], list[Skill], list[Subagent]]:
    rules: list[Rule] = []
    seen: set[str] = set()
    for sel in rule_names:
        for rule in resolve_rule_selector(master, sel):
            if rule.name not in seen:
                seen.add(rule.name)
                rules.append(rule)
    skills = [load_skill(master, n) for n in skill_names]
    subagents = [load_subagent(master, n) for n in subagent_names]
    return rules, skills, subagents


def filter_rules_for_agent(rules: list[Rule], agent: str) -> list[Rule]:
    return [r for r in rules if _rule_applies_to_agent(r.frontmatter, agent)]
