import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from myai.agentsync.config import MYAI_DIR
from myai.paths import global_sandbox_config_path, global_sandbox_config_write_path, sandbox_root

CONFIG_VERSION = 2
SANDBOX_CONFIG_FILE = "sandbox.json"

DEFAULT_MODEL_ENDPOINT = "http://localhost:8080/v1"
DEFAULT_GUEST_MODEL_HOST = "model.host"
DEFAULT_GONDOLIN_PACKAGE = "@earendil-works/gondolin"
DEFAULT_GONDOLIN_VERSION = "0.12.0"
DEFAULT_GUEST_HIDDEN_PATHS = ("/.myai",)
# Always hidden, whatever the config says. sandbox.json decides what the guest may
# reach, so a guest that could edit it would be granting itself the next run.
MANDATORY_GUEST_HIDDEN_PATHS = ("/.myai",)
DEFAULT_PI_PACKAGE = "@earendil-works/pi-coding-agent"

# These pick code that runs on the host (the sidecar's SDK) or lands in the pi
# cache shared by every repo, so a repo's sandbox.json never gets a say.
GLOBAL_ONLY_KEYS = ("gondolin_package", "gondolin_version", "pi_package")
DEFAULT_IMAGE = "alpine-base:latest"
DEFAULT_PROVIDER = "myai-local"
DEFAULT_MODEL_ID = "local"

_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*"
_NPM_NAME_RE = re.compile(rf"^{_NPM_NAME}$")
# name, optionally pinned to a version or dist-tag. No URLs, git refs, or paths.
_NPM_SPEC_RE = re.compile(rf"^{_NPM_NAME}(?:@[A-Za-z0-9][A-Za-z0-9._+-]*)?$")
_EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOST_LABELS = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
_HOST_RE = re.compile(rf"^{_HOST_LABELS}(?::\d{{1,5}})?$")
_WILDCARD_HOST_RE = re.compile(rf"^\*\.{_HOST_LABELS}(?::\d{{1,5}})?$")

# A secret's name becomes an env var in the host sidecar process too, so it must
# not be one that changes how that process loads code or finds programs.
_RESERVED_SECRET_NAMES = frozenset({
    "PATH", "HOME", "TERM", "SHELL", "SSH_AUTH_SOCK",
    "NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
})

_ROOTFS_SIZE_RE = re.compile(
    r"^\d+\s*([kKmMgGtTpPeE]?)(i?[bB])?$",
    re.IGNORECASE,
)

GUEST_AGENT_PATH = "/root/.pi/agent"
PI_INSTALL_MOUNT = "/opt/pi"
WORKSPACE_PATH = "/workspace"

VMM_CHOICES = ("auto", "qemu", "krun")
GUEST_REPO_MOUNT_CHOICES = ("host_path", "workspace")

NETWORK_POLICY_CHOICES = ("custom", "deny-all", "allow-all")
DEFAULT_NETWORK_POLICY = "custom"

# read-only: .git is readable but not writable (agent cannot commit).
# commit:    .git stays read-only, but the agent commits into a scratch clone
#            whose results the host imports to refs/sandbox/* after the run.
# write:     the real .git is writable (hooks/config there run on the host) — an
#            escape hatch, not a default.
GIT_ACCESS_CHOICES = ("read-only", "commit", "write")
DEFAULT_GIT_ACCESS = "read-only"
AGENT_GIT_MOUNT = "/root/agent-git"
GIT_BUNDLE_MOUNT = "/opt/git"
SANDBOX_REF_NAMESPACE = "refs/sandbox"

# gondolin only installs egress hooks when given >=1 --allow-host; with zero
# flags the network is wide open. Passing a host the guest never contacts forces
# the hooks on, so deny-all actually denies.
DENY_ALL_SENTINEL = "deny-all.sandbox.invalid"

# Known providers mapped to the domains they need, so users allow a name instead
# of hand-listing hosts.
PROVIDER_DOMAINS: dict[str, list[str]] = {
    "anthropic": ["api.anthropic.com"],
    "openai": ["api.openai.com"],
    "openrouter": ["openrouter.ai"],
    "gemini": [
        "generativelanguage.googleapis.com",
        "oauth2.googleapis.com",
        "www.googleapis.com",
    ],
    "github-copilot": [
        "*.githubcopilot.com",
        "api.github.com",
        "copilot-proxy.githubusercontent.com",
    ],
    "github": ["github.com", "*.github.com", "*.githubusercontent.com"],
    "ollama": ["localhost", "127.0.0.1"],
    "llama.cpp": ["localhost", "127.0.0.1"],
}


class SandboxConfigError(Exception):
    pass


def validate_host_pattern(pattern: object, *, what: str) -> None:
    """Accept a hostname or a leading-label wildcard; reject anything broader.

    Gondolin treats ``*`` as "any substring", dots included, so ``*`` alone is the
    whole internet, ``*.com`` nearly so, and ``*github.com`` matches
    ``evilgithub.com``. Only ``*.`` in front of at least two labels is allowed.
    """
    if not isinstance(pattern, str) or not pattern:
        raise SandboxConfigError(f"{what}: host must be a non-empty string, got {pattern!r}")
    if "*" not in pattern:
        if not _HOST_RE.match(pattern):
            raise SandboxConfigError(f"{what}: invalid host {pattern!r}")
        return
    if not _WILDCARD_HOST_RE.match(pattern) or pattern.split(":")[0].count(".") < 2:
        raise SandboxConfigError(
            f"{what}: wildcard {pattern!r} is too broad; use the form *.example.com "
            "(for unrestricted egress set network_policy to 'allow-all')"
        )


@dataclass
class HostSecret:
    name: str
    hosts: list[str]
    env_var: str | None = None

    def validate(self) -> None:
        if not self.name:
            raise SandboxConfigError("host secret name is required")
        if not _ENV_NAME_RE.match(self.name):
            raise SandboxConfigError(f"host secret name {self.name!r} is not a valid env var name")
        if self.name.upper() in _RESERVED_SECRET_NAMES:
            raise SandboxConfigError(f"host secret name {self.name!r} is reserved")
        if self.env_var is not None and (
            not isinstance(self.env_var, str) or not _ENV_NAME_RE.match(self.env_var)
        ):
            raise SandboxConfigError(
                f"host secret {self.name!r}: env_var {self.env_var!r} is not a valid env var name"
            )
        if not self.hosts:
            raise SandboxConfigError(f"host secret {self.name!r} needs at least one host")
        for host in self.hosts:
            validate_host_pattern(host, what=f"host secret {self.name!r}")


@dataclass
class RouteProvision:
    provider: str = DEFAULT_PROVIDER
    model_id: str = DEFAULT_MODEL_ID


@dataclass
class HostLoopbackRoute:
    id: str
    guest_host: str
    upstream: str
    provision: RouteProvision | None = None

    def validate(self) -> None:
        if not self.id:
            raise SandboxConfigError("host_loopback route id is required")
        if not self.guest_host or "." not in self.guest_host:
            raise SandboxConfigError(f"route {self.id!r}: guest_host must be a hostname")
        if not self.upstream:
            raise SandboxConfigError(f"route {self.id!r}: upstream is required")


@dataclass
class HostLoopbackConfig:
    enabled: bool = False
    routes: list[HostLoopbackRoute] = field(default_factory=list)
    # Routes reach this machine only unless set; then LAN hosts are allowed too.
    allow_remote_upstreams: bool = False

    def validate(self) -> None:
        seen: set[str] = set()
        provision_count = 0
        for route in self.routes:
            route.validate()
            if route.id in seen:
                raise SandboxConfigError(f"duplicate host_loopback route id {route.id!r}")
            seen.add(route.id)
            if route.provision:
                provision_count += 1
        if provision_count > 1:
            raise SandboxConfigError("only one host_loopback route may have provision")


@dataclass
class SandboxConfig:
    version: int = CONFIG_VERSION
    model_endpoint: str = DEFAULT_MODEL_ENDPOINT
    guest_model_host: str = DEFAULT_GUEST_MODEL_HOST
    model_id: str = DEFAULT_MODEL_ID
    provider: str = DEFAULT_PROVIDER
    network_policy: str = DEFAULT_NETWORK_POLICY
    providers: list[str] = field(default_factory=list)
    allow_hosts: list[str] = field(default_factory=list)
    auto_approve: bool = True
    gondolin_package: str = DEFAULT_GONDOLIN_PACKAGE
    gondolin_version: str = DEFAULT_GONDOLIN_VERSION
    image: str = DEFAULT_IMAGE
    rootfs_size: str | None = None
    vmm: str = "auto"
    mount_readonly: bool = False
    # workspace-relative paths hidden from the guest (ShadowProvider deny+hide)
    guest_hidden_paths: list[str] = field(default_factory=lambda: list(DEFAULT_GUEST_HIDDEN_PATHS))
    # how the guest may use git; see GIT_ACCESS_CHOICES
    git_access: str = DEFAULT_GIT_ACCESS
    install_pi_at_boot: bool = True
    pi_package: str = DEFAULT_PI_PACKAGE
    mirror_host_pi: bool = False
    llama_server_url: str | None = None
    # bind-mount host ~/.pi/agent/sessions into the guest so pi sessions are shared
    share_host_sessions: bool = True
    # host_path: mount repo at its real absolute path (seamless cross-resume, leaks path)
    # workspace: mount at /workspace (no leak; cross-resume cwd may not line up)
    guest_repo_mount: str = "host_path"
    host_secrets: list[HostSecret] = field(default_factory=list)
    ssh_allow_hosts: list[str] = field(default_factory=list)
    use_ssh_agent: bool = False
    host_loopback: HostLoopbackConfig = field(default_factory=HostLoopbackConfig)
    # Notes from loading (ignored repo keys and the like). Not part of the config.
    warnings: list[str] = field(default_factory=list, compare=False, repr=False)

    def validate(self) -> None:
        if self.vmm not in VMM_CHOICES:
            raise SandboxConfigError(f"unknown vmm {self.vmm!r}, expected one of {VMM_CHOICES}")
        if self.network_policy not in NETWORK_POLICY_CHOICES:
            raise SandboxConfigError(
                f"unknown network_policy {self.network_policy!r}, "
                f"expected one of {NETWORK_POLICY_CHOICES}"
            )
        for provider in self.providers:
            if provider not in PROVIDER_DOMAINS:
                known = ", ".join(sorted(PROVIDER_DOMAINS))
                raise SandboxConfigError(f"unknown provider {provider!r}; known: {known}")
        for host in self.allow_hosts:
            validate_host_pattern(host, what="allow_hosts")
        for host in self.ssh_allow_hosts:
            validate_host_pattern(host, what="ssh_allow_hosts")
        if not isinstance(self.gondolin_package, str) or not _NPM_NAME_RE.match(self.gondolin_package):
            raise SandboxConfigError(
                f"invalid gondolin_package {self.gondolin_package!r}; expected an npm package name"
            )
        if self.gondolin_version != "latest" and (
            not isinstance(self.gondolin_version, str)
            or not _EXACT_VERSION_RE.match(self.gondolin_version)
        ):
            raise SandboxConfigError(
                f"invalid gondolin_version {self.gondolin_version!r}; "
                "use an exact version like 0.12.0 (URLs, ranges, and git refs are not accepted)"
            )
        if not isinstance(self.pi_package, str) or not _NPM_SPEC_RE.match(self.pi_package):
            raise SandboxConfigError(
                f"invalid pi_package {self.pi_package!r}; expected an npm package name, "
                "optionally with @version"
            )
        if self.guest_repo_mount not in GUEST_REPO_MOUNT_CHOICES:
            raise SandboxConfigError(
                f"unknown guest_repo_mount {self.guest_repo_mount!r}, "
                f"expected one of {GUEST_REPO_MOUNT_CHOICES}"
            )
        if self.git_access not in GIT_ACCESS_CHOICES:
            raise SandboxConfigError(
                f"unknown git_access {self.git_access!r}, expected one of {GIT_ACCESS_CHOICES}"
            )
        if self.rootfs_size is not None and not _ROOTFS_SIZE_RE.match(self.rootfs_size.strip()):
            raise SandboxConfigError(
                f"invalid rootfs_size {self.rootfs_size!r}; use a size like 4G or 512M"
            )
        for path in self.guest_hidden_paths:
            if not path.startswith("/"):
                raise SandboxConfigError(
                    f"guest_hidden_paths entries must be absolute workspace paths, got {path!r}"
                )
        self.host_loopback.validate()
        for secret in self.host_secrets:
            secret.validate()
        if self.host_loopback.enabled:
            # Resolve regardless of env/policy switches so a bad route is reported
            # where it is configured, not on some later run that flips one.
            if not _resolve_routes(self):
                raise SandboxConfigError(
                    "host_loopback.enabled is true but no routes are configured"
                )


def repo_config_path(repo: Path) -> Path:
    return repo / MYAI_DIR / SANDBOX_CONFIG_FILE


def _git_access_from_dict(data: dict) -> str:
    if "git_access" in data:
        return str(data["git_access"])
    # guest_git_readonly shipped only on the unreleased hardening branch; honor it
    # so an early adopter's config keeps working.
    if "guest_git_readonly" in data:
        return "read-only" if data["guest_git_readonly"] else "write"
    return DEFAULT_GIT_ACCESS


def real_git_readonly(cfg: "SandboxConfig") -> bool:
    """True when the guest must not write the real .git (every mode but 'write')."""
    return cfg.git_access != "write"


def git_commit_mode(cfg: "SandboxConfig") -> bool:
    return cfg.git_access == "commit"


def resolve_model_endpoint(cfg: SandboxConfig | None = None) -> str:
    if env := os.environ.get("MYAI_MODEL_ENDPOINT"):
        return env.strip()
    if cfg and cfg.model_endpoint:
        return cfg.model_endpoint
    return DEFAULT_MODEL_ENDPOINT


def resolve_host_loopback_enabled(cfg: SandboxConfig) -> bool:
    # deny-all means no network at all, host ports included. "Only my local
    # model" is the custom policy with an empty allow list plus loopback.
    if cfg.network_policy == "deny-all":
        return False
    env = os.environ.get("MYAI_HOST_LOOPBACK")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return cfg.host_loopback.enabled


@dataclass(frozen=True)
class GuestEndpoint:
    host_endpoint: str
    guest_endpoint: str
    guest_host: str
    port: int
    scheme: str
    path: str


@dataclass(frozen=True)
class ResolvedRoute:
    route: HostLoopbackRoute
    guest: GuestEndpoint
    upstream_host: str
    upstream_port: int


def parse_upstream(upstream: str) -> tuple[str, int, str | None]:
    """Return (host, port, url_for_rewrite or None)."""
    upstream = upstream.strip()
    if "://" in upstream:
        parsed = urlparse(upstream)
        if not parsed.hostname:
            raise SandboxConfigError(f"invalid upstream URL: {upstream!r}")
        host = _normalize_loopback_host(parsed.hostname)
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return host, port, upstream

    if ":" in upstream:
        host_part, port_part = upstream.rsplit(":", 1)
        try:
            port = int(port_part)
        except ValueError as exc:
            raise SandboxConfigError(f"invalid upstream port in {upstream!r}") from exc
        return _normalize_loopback_host(host_part), port, None

    raise SandboxConfigError(
        f"invalid upstream {upstream!r}; use a URL or host:port"
    )


def _normalize_loopback_host(host: str) -> str:
    if host in ("localhost", "127.0.0.1", "::1"):
        return "127.0.0.1"
    return host


def _check_upstream_host(route_id: str, host: str, *, allow_remote: bool) -> None:
    """Loopback routes bridge the guest to a host-side port; keep that to this
    machine unless the user opted in, and never to link-local (cloud metadata)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback:
            return
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise SandboxConfigError(
                f"route {route_id!r}: upstream {host!r} is not allowed "
                "(link-local, multicast, and unspecified addresses are never bridged)"
            )
    elif host == "localhost":
        return
    if not allow_remote:
        raise SandboxConfigError(
            f"route {route_id!r}: upstream {host!r} is not this machine; set "
            "host_loopback.allow_remote_upstreams (or --allow-remote-upstream) to bridge "
            "the guest to other hosts"
        )


def rewrite_endpoint_for_guest(
    host_endpoint: str,
    guest_host: str = DEFAULT_GUEST_MODEL_HOST,
) -> GuestEndpoint:
    parsed = urlparse(host_endpoint)
    if not parsed.scheme or not parsed.hostname:
        raise SandboxConfigError(f"invalid endpoint URL: {host_endpoint!r}")

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    path = parsed.path or ""
    guest_netloc = f"{guest_host}:{port}"
    guest_endpoint = urlunparse((parsed.scheme, guest_netloc, path, "", "", ""))

    return GuestEndpoint(
        host_endpoint=host_endpoint,
        guest_endpoint=guest_endpoint,
        guest_host=guest_host,
        port=port,
        scheme=parsed.scheme,
        path=path,
    )


def resolve_host_loopback_routes(cfg: SandboxConfig) -> list[ResolvedRoute]:
    if not resolve_host_loopback_enabled(cfg):
        return []
    return _resolve_routes(cfg)


def _resolve_routes(cfg: SandboxConfig) -> list[ResolvedRoute]:
    routes = list(cfg.host_loopback.routes)
    if not routes:
        endpoint = resolve_model_endpoint(cfg)
        routes = [
            HostLoopbackRoute(
                id="model",
                guest_host=cfg.guest_model_host,
                upstream=endpoint,
                provision=RouteProvision(
                    provider=cfg.provider,
                    model_id=cfg.model_id,
                ),
            )
        ]

    resolved: list[ResolvedRoute] = []
    for route in routes:
        host, port, url = parse_upstream(route.upstream)
        _check_upstream_host(
            route.id, host, allow_remote=cfg.host_loopback.allow_remote_upstreams
        )
        if url:
            guest = rewrite_endpoint_for_guest(url, route.guest_host)
        else:
            guest = GuestEndpoint(
                host_endpoint=f"tcp://{host}:{port}",
                guest_endpoint=f"tcp://{route.guest_host}:{port}",
                guest_host=route.guest_host,
                port=port,
                scheme="tcp",
                path="",
            )
        resolved.append(
            ResolvedRoute(
                route=route,
                guest=guest,
                upstream_host=host,
                upstream_port=port,
            )
        )
    return resolved


def provision_route(cfg: SandboxConfig) -> ResolvedRoute | None:
    for resolved in resolve_host_loopback_routes(cfg):
        if resolved.route.provision:
            return resolved
    return None


def _host_secret_from_dict(data: dict) -> HostSecret:
    hosts = data.get("hosts", [])
    if isinstance(hosts, str):
        hosts = [h.strip() for h in hosts.split(",") if h.strip()]
    return HostSecret(
        name=str(data.get("name", "")),
        hosts=[str(h) for h in hosts],
        env_var=data.get("env_var"),
    )


def _provision_from_dict(data: dict | None) -> RouteProvision | None:
    if not data:
        return None
    return RouteProvision(
        provider=data.get("provider", DEFAULT_PROVIDER),
        model_id=data.get("model_id", DEFAULT_MODEL_ID),
    )


def _route_from_dict(data: dict) -> HostLoopbackRoute:
    return HostLoopbackRoute(
        id=str(data.get("id", "")),
        guest_host=str(data.get("guest_host", "")),
        upstream=str(data.get("upstream", "")),
        provision=_provision_from_dict(data.get("provision")),
    )


def _host_loopback_from_dict(data: dict | None) -> HostLoopbackConfig:
    if not data:
        return HostLoopbackConfig()
    routes = [_route_from_dict(r) for r in data.get("routes", [])]
    return HostLoopbackConfig(
        enabled=bool(data.get("enabled", False)),
        routes=routes,
        allow_remote_upstreams=bool(data.get("allow_remote_upstreams", False)),
    )


def _config_from_dict(data: dict) -> SandboxConfig:
    secrets = [_host_secret_from_dict(s) for s in data.get("host_secrets", [])]
    cfg = SandboxConfig(
        version=data.get("version", CONFIG_VERSION),
        model_endpoint=data.get("model_endpoint", DEFAULT_MODEL_ENDPOINT),
        guest_model_host=data.get("guest_model_host", DEFAULT_GUEST_MODEL_HOST),
        model_id=data.get("model_id", DEFAULT_MODEL_ID),
        provider=data.get("provider", DEFAULT_PROVIDER),
        network_policy=data.get("network_policy", DEFAULT_NETWORK_POLICY),
        providers=list(data.get("providers", [])),
        allow_hosts=list(data.get("allow_hosts", [])),
        auto_approve=data.get("auto_approve", True),
        gondolin_package=data.get("gondolin_package", DEFAULT_GONDOLIN_PACKAGE),
        gondolin_version=data.get("gondolin_version", DEFAULT_GONDOLIN_VERSION),
        image=data.get("image", DEFAULT_IMAGE),
        rootfs_size=data.get("rootfs_size"),
        vmm=data.get("vmm", "auto"),
        mount_readonly=data.get("mount_readonly", False),
        guest_hidden_paths=list(data.get("guest_hidden_paths", DEFAULT_GUEST_HIDDEN_PATHS)),
        git_access=_git_access_from_dict(data),
        install_pi_at_boot=data.get("install_pi_at_boot", True),
        pi_package=data.get("pi_package", DEFAULT_PI_PACKAGE),
        mirror_host_pi=data.get("mirror_host_pi", False),
        llama_server_url=data.get("llama_server_url"),
        share_host_sessions=data.get("share_host_sessions", True),
        guest_repo_mount=data.get("guest_repo_mount", "host_path"),
        host_secrets=secrets,
        ssh_allow_hosts=list(data.get("ssh_allow_hosts", [])),
        use_ssh_agent=data.get("use_ssh_agent", False),
        host_loopback=_host_loopback_from_dict(data.get("host_loopback")),
    )
    cfg.validate()
    return cfg


def _read_config_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SandboxConfigError(f"invalid sandbox config at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SandboxConfigError(f"invalid sandbox config at {path}: expected an object")
    return data


def _global_config_file() -> Path | None:
    path = global_sandbox_config_path()
    if path is not None and path.is_file():
        return path
    legacy = sandbox_root() / SANDBOX_CONFIG_FILE
    return legacy if legacy.is_file() else None


def load_global_config() -> SandboxConfig | None:
    path = _global_config_file()
    if path is None:
        return None
    return _config_from_dict(_read_config_file(path))


def _strip_global_only(repo_data: dict, global_data: dict) -> tuple[dict, list[str]]:
    """Drop GLOBAL_ONLY_KEYS from a repo config; say so when they would have mattered."""
    out = dict(repo_data)
    warnings: list[str] = []
    defaults = SandboxConfig()
    for key in GLOBAL_ONLY_KEYS:
        if key not in out:
            continue
        value = out.pop(key)
        effective = global_data.get(key, getattr(defaults, key))
        if value != effective:
            warnings.append(
                f"ignored {key}={value!r} from the repo's sandbox.json; "
                f"it can only be set in the global config (using {effective!r})"
            )
    return out, warnings


def load_config(repo: Path | None = None, *, repo_data: dict | None = None) -> SandboxConfig:
    """Global config with the repo's layered on top.

    Merging happens on the raw files, so the repo overrides only the keys it
    actually names; a sparse repo file leaves the user's global choices alone.
    ``repo_data`` lets a caller that already read (and vetted) the repo file pass
    those exact bytes' contents instead of having them re-read here.
    """
    global_path = _global_config_file()
    global_data = _read_config_file(global_path) if global_path else {}

    if repo_data is None and repo is not None:
        path = repo_config_path(repo)
        if path.is_file():
            repo_data = _read_config_file(path)

    warnings: list[str] = []
    if repo_data is not None:
        repo_data, warnings = _strip_global_only(repo_data, global_data)
        merged = _merge_dicts(global_data, repo_data)
    else:
        merged = global_data

    cfg = _config_from_dict(merged)
    cfg.warnings = warnings
    return cfg


def save_global_config(cfg: SandboxConfig) -> None:
    cfg.validate()
    path = global_sandbox_config_write_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_config_to_dict(cfg), indent=2) + "\n", encoding="utf-8")


def save_repo_config(repo: Path, cfg: SandboxConfig) -> None:
    cfg.validate()
    path = repo_config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in _config_to_dict(cfg).items() if k not in GLOBAL_ONLY_KEYS}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _config_to_dict(cfg: SandboxConfig) -> dict:
    return {
        "version": cfg.version,
        "host_loopback": {
            "enabled": cfg.host_loopback.enabled,
            **(
                {"allow_remote_upstreams": True}
                if cfg.host_loopback.allow_remote_upstreams
                else {}
            ),
            "routes": [
                {
                    "id": r.id,
                    "guest_host": r.guest_host,
                    "upstream": r.upstream,
                    **(
                        {
                            "provision": {
                                "provider": r.provision.provider,
                                "model_id": r.provision.model_id,
                            }
                        }
                        if r.provision
                        else {}
                    ),
                }
                for r in cfg.host_loopback.routes
            ],
        },
        "model_endpoint": cfg.model_endpoint,
        "guest_model_host": cfg.guest_model_host,
        "model_id": cfg.model_id,
        "provider": cfg.provider,
        "network_policy": cfg.network_policy,
        "providers": cfg.providers,
        "allow_hosts": cfg.allow_hosts,
        "auto_approve": cfg.auto_approve,
        "gondolin_package": cfg.gondolin_package,
        "gondolin_version": cfg.gondolin_version,
        "image": cfg.image,
        **({"rootfs_size": cfg.rootfs_size} if cfg.rootfs_size else {}),
        "vmm": cfg.vmm,
        "mount_readonly": cfg.mount_readonly,
        "guest_hidden_paths": cfg.guest_hidden_paths,
        "git_access": cfg.git_access,
        "install_pi_at_boot": cfg.install_pi_at_boot,
        "pi_package": cfg.pi_package,
        "mirror_host_pi": cfg.mirror_host_pi,
        **({"llama_server_url": cfg.llama_server_url} if cfg.llama_server_url else {}),
        "share_host_sessions": cfg.share_host_sessions,
        "guest_repo_mount": cfg.guest_repo_mount,
        "host_secrets": [
            {
                "name": s.name,
                "hosts": s.hosts,
                **({"env_var": s.env_var} if s.env_var else {}),
            }
            for s in cfg.host_secrets
        ],
        "ssh_allow_hosts": cfg.ssh_allow_hosts,
        "use_ssh_agent": cfg.use_ssh_agent,
    }


def _merge_dicts(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key == "host_loopback" and key in out and isinstance(value, dict):
            out[key] = value
            continue
        if isinstance(value, list) and not value and key in out:
            continue
        out[key] = value
    return out


NPM_REGISTRY_HOST = "registry.npmjs.org"
PI_TOOL_DOWNLOAD_HOSTS = (
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
)
# apk mirror for installing git (needed by host-mirrored git packages)
ALPINE_MIRROR_HOST = "dl-cdn.alpinelinux.org"


def effective_rootfs_size(cfg: SandboxConfig) -> str | None:
    return cfg.rootfs_size


def _append_loopback_hosts(hosts: list[str], cfg: SandboxConfig) -> list[str]:
    if not resolve_host_loopback_enabled(cfg):
        return hosts
    for resolved in resolve_host_loopback_routes(cfg):
        guest = resolved.guest
        if guest.guest_host not in hosts:
            hosts.append(guest.guest_host)
        if guest.port not in (80, 443):
            port_pattern = f"{guest.guest_host}:{guest.port}"
            if port_pattern not in hosts:
                hosts.append(port_pattern)
    return hosts


def resolve_provider_domains(cfg: SandboxConfig) -> list[str]:
    """Expand cfg.providers into their domain lists, preserving order, deduped."""
    out: list[str] = []
    for provider in cfg.providers:
        for domain in PROVIDER_DOMAINS.get(provider, []):
            if domain not in out:
                out.append(domain)
    return out


def effective_allow_hosts(cfg: SandboxConfig) -> list[str]:
    """Runtime allow list: provider presets + user hosts + loopback hosts."""
    hosts: list[str] = []
    for host in (*resolve_provider_domains(cfg), *cfg.allow_hosts):
        if host not in hosts:
            hosts.append(host)
    return _append_loopback_hosts(hosts, cfg)


def runtime_allow_host_args(cfg: SandboxConfig) -> tuple[list[str], bool]:
    """Runtime --allow-host values and whether egress is unrestricted.

    For allow-all returns ([], True): pass no flags. Otherwise the list is never
    empty so gondolin installs hooks; an empty custom list collapses to the
    deny-all sentinel (fail-closed) instead of gondolin's open default.
    """
    if cfg.network_policy == "allow-all":
        return [], True
    if cfg.network_policy == "deny-all":
        return [DENY_ALL_SENTINEL], False
    hosts = effective_allow_hosts(cfg)
    return (hosts or [DENY_ALL_SENTINEL]), False


def provision_allow_hosts(cfg: SandboxConfig) -> list[str]:
    """Install-time allow list: user config + npm/github/alpine when boot-installing pi."""
    hosts = list(cfg.allow_hosts)
    if cfg.install_pi_at_boot and cfg.image == DEFAULT_IMAGE:
        for host in (NPM_REGISTRY_HOST, *PI_TOOL_DOWNLOAD_HOSTS):
            if host not in hosts:
                hosts.append(host)
        # git is apk-installed during provisioning (staged into a persistent
        # bundle for runtime), so the alpine mirror is always needed here.
        if ALPINE_MIRROR_HOST not in hosts:
            hosts.append(ALPINE_MIRROR_HOST)
    return hosts


def effective_hidden_paths(cfg: SandboxConfig) -> list[str]:
    """Hidden paths for the guest: the mandatory ones first, then the config's."""
    out: list[str] = []
    for path in (*MANDATORY_GUEST_HIDDEN_PATHS, *cfg.guest_hidden_paths):
        if path not in out:
            out.append(path)
    return out


def host_sessions_dir() -> Path:
    return Path.home() / ".pi" / "agent" / "sessions"


def effective_workspace_path(repo: Path, cfg: SandboxConfig) -> str:
    if cfg.guest_repo_mount == "workspace":
        return WORKSPACE_PATH
    return str(repo.resolve())


def sidecar_source_dir() -> Path:
    """Bundled Node sidecar sources shipped with the Python package."""
    return Path(__file__).resolve().parent / "sidecar"


def sidecar_install_dir() -> Path:
    """Host cache dir where npm installs the Gondolin SDK for the sidecar."""
    return sandbox_root() / "sidecar"


def gondolin_package_spec(cfg: SandboxConfig) -> str:
    """npm package spec for the pinned Gondolin SDK."""
    pkg = cfg.gondolin_package
    version = cfg.gondolin_version
    if version and version != "latest":
        return f"{pkg}@{version}"
    return pkg


def sidecar_invocation(cfg: SandboxConfig) -> list[str]:
    """Argv prefix to run the Node Gondolin sidecar."""
    install = sidecar_install_dir()
    script = install / "sidecar.mjs"
    return ["node", str(script)]


def default_sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        version=CONFIG_VERSION,
        host_loopback=HostLoopbackConfig(enabled=False, routes=[]),
    )
