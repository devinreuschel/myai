import hashlib
import json
import os
import shutil
from pathlib import Path

from myai.agentsync.config import ConfigError, load_config, resolve_inject_myai_rule
from myai.agentsync.master import filter_rules_for_agent, resolve_selection
from myai.agentsync.registry import get_master
from myai.agentsync.render import MYAI_MANAGED_RULE, render_rules_block
from myai.paths import sandbox_root
from myai.sandbox.config import (
    DEFAULT_IMAGE,
    DEFAULT_MODEL_ID,
    GUEST_AGENT_PATH,
    PI_INSTALL_MOUNT,
    WORKSPACE_PATH,
    SandboxConfig,
    host_sessions_dir,
    provision_route,
    resolve_host_loopback_enabled,
    resolve_host_loopback_routes,
)

# Guest-visible paths for the --debug missing-executable audit. The staging dir
# is host-mounted at GUEST_AGENT_PATH, so anything written here under .debug is
# readable on the host after the VM exits.
DEBUG_DIR = ".debug"
DEBUG_MISSING_EXES = "missing-exes.log"
DEBUG_INIT_SH = "init.sh"

# settings.json keys we mirror from the host pi install into the guest
HOST_PI_SETTINGS_KEYS = (
    "packages",
    "defaultProvider",
    "defaultModel",
    "defaultThinkingLevel",
    "theme",
)


class ProvisionError(Exception):
    pass


def agent_staging_dir(repo: Path) -> Path:
    key = str(repo.resolve())
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return sandbox_root() / "agent" / digest


def pi_install_dir() -> Path:
    return sandbox_root() / "pi-prefix"


def pi_bin_dir() -> Path:
    return sandbox_root() / "pi-bin"


def pi_pkg_dir(name: str) -> Path:
    return sandbox_root() / "pi-pkgs" / name


def is_provisioned(cfg: SandboxConfig) -> bool:
    if not (cfg.install_pi_at_boot and cfg.image == DEFAULT_IMAGE):
        return True
    pi_bin = pi_install_dir() / "node_modules" / ".bin" / "pi"
    if not pi_bin.is_file():
        return False
    for tool in ("fd", "rg"):
        if not (pi_bin_dir() / tool).is_file():
            return False
    return True


def needs_provision(cfg: SandboxConfig, *, force: bool = False) -> bool:
    if not (cfg.install_pi_at_boot and cfg.image == DEFAULT_IMAGE):
        return False
    if force:
        return True
    return not is_provisioned(cfg)


def host_pi_settings_path() -> Path:
    return Path.home() / ".pi" / "agent" / "settings.json"


def prepare_agent_dir(repo: Path, cfg: SandboxConfig, *, debug: bool = False) -> Path:
    staging = agent_staging_dir(repo)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / "models.json").write_text(render_models_json(cfg), encoding="utf-8")
    settings = render_guest_settings(cfg)
    if settings:
        (staging / "settings.json").write_text(settings, encoding="utf-8")
    agents_md = render_agents_md(repo)
    if agents_md:
        (staging / "AGENTS.md").write_text(agents_md, encoding="utf-8")
    system_md = render_global_system_md()
    if system_md:
        (staging / "SYSTEM.md").write_text(system_md, encoding="utf-8")
    append_system = render_myai_append_system(repo)
    if append_system:
        (staging / "APPEND_SYSTEM.md").write_text(append_system, encoding="utf-8")
    # sessions/ is a placeholder; when share_host_sessions is on the sidecar
    # overlays host ~/.pi/agent/sessions at GUEST_AGENT_PATH/sessions. A host
    # symlink does not work: RealFSProvider cannot follow targets outside the
    # staging mount root, so guest mkdir fails with ENOENT.
    (staging / "sessions").mkdir(exist_ok=True)
    if debug:
        _write_debug_init(staging)
    return staging


def _write_debug_init(staging: Path) -> None:
    """Write a bash init script that logs every command the guest can't find.

    Sourced via ``BASH_ENV`` on every non-interactive bash, which covers the
    ``bash -c`` calls pi makes for its shell tool. The log lives in the
    host-mounted staging dir so we can read it back after the VM exits.
    """
    debug_dir = staging / DEBUG_DIR
    debug_dir.mkdir(exist_ok=True)
    (debug_dir / DEBUG_MISSING_EXES).write_text("", encoding="utf-8")
    guest_log = f"{GUEST_AGENT_PATH}/{DEBUG_DIR}/{DEBUG_MISSING_EXES}"
    script = (
        "command_not_found_handle() {\n"
        f'  printf "%s\\n" "$1" >> {guest_log}\n'
        "  return 127\n"
        "}\n"
    )
    (debug_dir / DEBUG_INIT_SH).write_text(script, encoding="utf-8")


def read_debug_missing_exes(staging: Path) -> list[str]:
    """Return sorted unique executables the guest failed to find, if any."""
    log = staging / DEBUG_DIR / DEBUG_MISSING_EXES
    if not log.is_file():
        return []
    names = {line.strip() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()}
    return sorted(names)


def session_dir_name(path: str) -> str:
    """Encode an absolute path to pi's session directory name (``--a-b-c--``)."""
    inner = path.lstrip("/").replace("/", "-")
    return f"--{inner}--"


def prepare_workspace_session_link(repo: Path, cfg: SandboxConfig) -> Path | None:
    """Link the ``/workspace`` session slot to the repo's real session dir.

    In ``workspace`` mount mode the guest cwd is ``/workspace``, so pi names its
    session dir ``--workspace--`` instead of one derived from the repo path. With
    shared host sessions that would orphan sessions in a slot unrelated to the
    repo. We symlink ``--workspace--`` to the repo's real session dir so host and
    guest share one pool. No-op for ``host_path`` mode or when sessions aren't
    shared. Returns the link path to clean up, or None.
    """
    if cfg.guest_repo_mount != "workspace" or not cfg.share_host_sessions:
        return None
    sessions = host_sessions_dir()
    sessions.mkdir(parents=True, exist_ok=True)
    link = sessions / session_dir_name(WORKSPACE_PATH)
    target = sessions / session_dir_name(str(repo.resolve()))
    target.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()  # stale link from a crashed run
    elif link.exists():
        return None  # real dir already there, leave it
    link.symlink_to(target)
    return link


def cleanup_workspace_session_link(link: Path | None) -> None:
    """Remove the ``--workspace--`` symlink created by prepare_workspace_session_link."""
    if link is not None and link.is_symlink():
        link.unlink()


def loopback_guest_host(cfg: SandboxConfig) -> str | None:
    """Guest hostname the provision route is reachable at (e.g. model.host)."""
    route = provision_route(cfg)
    if route:
        return route.guest.guest_host
    routes = resolve_host_loopback_routes(cfg)
    return routes[0].guest.guest_host if routes else None


def _rewrite_localhost(value: str, guest_host: str) -> str:
    # host's 127.0.0.1/localhost is the host; inside the guest those point at the
    # guest itself, so swap for the loopback host that tcp-map bridges back.
    return value.replace("127.0.0.1", guest_host).replace("localhost", guest_host)


def render_guest_settings(cfg: SandboxConfig) -> str | None:
    if not cfg.mirror_host_pi:
        return None
    path = host_pi_settings_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    out: dict = {}
    for key in HOST_PI_SETTINGS_KEYS:
        if key in data:
            out[key] = data[key]
    if not out:
        return None

    guest_host = loopback_guest_host(cfg)
    if guest_host and isinstance(out.get("defaultProvider"), str):
        out["defaultProvider"] = _rewrite_localhost(out["defaultProvider"], guest_host)
    return json.dumps(out, indent=2) + "\n"


def render_models_json(cfg: SandboxConfig) -> str:
    if not resolve_host_loopback_enabled(cfg):
        return "{}\n"

    providers: dict = {}
    for resolved in resolve_host_loopback_routes(cfg):
        prov = resolved.route.provision
        if not prov:
            continue
        if "://" in resolved.route.upstream:
            base_url = resolved.guest.guest_endpoint.rstrip("/")
        else:
            # host:port only — no models.json entry unless provisioned with URL upstream
            continue
        providers[prov.provider] = {
            "baseUrl": base_url,
            "api": "openai-completions",
            "apiKey": "local",
            "compat": {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": False,
                "supportsUsageInStreaming": False,
            },
            "models": [
                {
                    "id": prov.model_id or DEFAULT_MODEL_ID,
                    "name": f"Local ({prov.provider})",
                    "reasoning": False,
                    "input": ["text"],
                    "contextWindow": 128000,
                    "maxTokens": 16384,
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                }
            ],
        }
    return json.dumps({"providers": providers}, indent=2) + "\n"


def render_agents_md(repo: Path) -> str | None:
    """Flatten the repo's pi rules for the guest, or None when not applicable.

    Only managed repos that don't already target pi get rules injected; a
    pi-targeting repo already carries its own synced AGENTS.md in the workspace.
    """
    try:
        cfg = load_config(repo)
    except ConfigError:
        return None
    if not cfg.managed or "pi" in cfg.agents:
        return None
    master = get_master()
    if master is None or not master.is_dir():
        return None
    try:
        rules, _, _ = resolve_selection(master, cfg.rules, [], [])
        pi_rules = filter_rules_for_agent(rules, "pi")
        block = render_rules_block(pi_rules, "Project rules (myai)")
        return block.strip() or None
    except Exception:
        return None


def render_myai_append_system(repo: Path) -> str | None:
    """Inject the myai-managed guardrail into pi's system prompt for sandbox runs.

    Only managed repos that don't already target pi get this in the guest agent
    dir; pi-targeting repos receive it via sync into .pi/APPEND_SYSTEM.md.
    """
    try:
        cfg = load_config(repo)
    except ConfigError:
        return None
    if not cfg.managed or "pi" in cfg.agents:
        return None
    if not resolve_inject_myai_rule(cfg):
        return None
    return MYAI_MANAGED_RULE


def render_global_system_md() -> str | None:
    master = get_master()
    if master is None:
        return None
    system = master / "SYSTEM.md"
    if system.is_file():
        return system.read_text(encoding="utf-8").strip() or None
    return None


def guest_agent_env(cfg: SandboxConfig, *, debug: bool = False) -> list[str]:
    env = [f"PI_CODING_AGENT_DIR={GUEST_AGENT_PATH}"]
    # forward TERM so the guest matches the real terminal for color/capability detection
    env.append(f"TERM={os.environ.get('TERM') or 'xterm-256color'}")
    if cfg.llama_server_url:
        url = cfg.llama_server_url
        guest_host = loopback_guest_host(cfg)
        if guest_host:
            url = _rewrite_localhost(url, guest_host)
        env.append(f"LLAMA_SERVER_URL={url}")
    if debug:
        env.append(f"BASH_ENV={GUEST_AGENT_PATH}/{DEBUG_DIR}/{DEBUG_INIT_SH}")
    return env


def build_provision_shell(cfg: SandboxConfig) -> tuple[str, list[str]]:
    """One-shot install script: npm pi, pre-fetch fd/rg, optional package sync."""
    tools_js = (
        "/opt/pi/node_modules/@earendil-works/pi-coding-agent/dist/utils/tools-manager.js"
    )
    git_setup = (
        "command -v git >/dev/null 2>&1 || apk add --no-cache git >/dev/null 2>&1; "
        if cfg.mirror_host_pi
        else ""
    )
    pkg_sync = (
        f'export PI_CODING_AGENT_DIR={GUEST_AGENT_PATH}; '
        '"$PI_BIN" update --extensions -a >/dev/null 2>&1 || true; '
        if cfg.mirror_host_pi
        else ""
    )
    script = (
        "set -e; "
        f"PI_PREFIX={PI_INSTALL_MOUNT}; "
        'PI_BIN="$PI_PREFIX/node_modules/.bin/pi"; '
        'export npm_config_cache="$PI_PREFIX/.npm-cache"; '
        'mkdir -p "$PI_PREFIX" "$npm_config_cache"; '
        'if ! [ -x "$PI_BIN" ]; then '
        f'npm install --prefix "$PI_PREFIX" --ignore-scripts {cfg.pi_package}; '
        "fi; "
        + git_setup
        + f'export PI_CODING_AGENT_DIR={GUEST_AGENT_PATH}; '
        f'node -e "import(\'{tools_js}\').then(m=>Promise.all([m.ensureTool(\'fd\'),m.ensureTool(\'rg\')]))"; '
        + pkg_sync
    )
    return "sh", ["-lc", script]


def build_pi_launch_shell(
    cfg: SandboxConfig,
    pi_args: list[str],
    workspace_path: str,
) -> tuple[str, list[str]]:
    args = list(pi_args)
    if cfg.auto_approve and "-a" not in args and "--approve" not in args:
        args = ["-a", *args]

    # When mirroring host pi, the host settings.json drives provider/model so we
    # don't inject our synthetic --provider/--model.
    if resolve_host_loopback_enabled(cfg) and not cfg.mirror_host_pi:
        prov = provision_route(cfg)
        if prov and prov.route.provision:
            p = prov.route.provision
            if not any(a.startswith("--provider") for a in args):
                args = ["--provider", p.provider, *args]
            if not any(a.startswith("--model") for a in args):
                args = ["--model", p.model_id or DEFAULT_MODEL_ID, *args]

    if cfg.install_pi_at_boot and cfg.image == DEFAULT_IMAGE:
        quoted_args = " ".join(_shell_quote(a) for a in args)
        script = (
            "set -e; "
            f'PI_BIN="{PI_INSTALL_MOUNT}/node_modules/.bin/pi"; '
            f"cd {_shell_quote(workspace_path)} && "
            f'exec "$PI_BIN" {quoted_args}'
        )
        return "sh", ["-lc", script]

    return "pi", args


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(c.isalnum() or c in "/._-:" for c in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
