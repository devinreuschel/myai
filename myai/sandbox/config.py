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
DEFAULT_GONDOLIN_VERSION = "latest"
DEFAULT_IMAGE = "alpine-base:latest"
DEFAULT_PROVIDER = "myai-local"
DEFAULT_MODEL_ID = "local"

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


@dataclass
class HostSecret:
    name: str
    hosts: list[str]
    env_var: str | None = None

    def validate(self) -> None:
        if not self.name:
            raise SandboxConfigError("host secret name is required")
        if not self.hosts:
            raise SandboxConfigError(f"host secret {self.name!r} needs at least one host")


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
    warm_reuse: bool = True
    mount_readonly: bool = False
    install_pi_at_boot: bool = True
    pi_package: str = "@earendil-works/pi-coding-agent"
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
        if self.guest_repo_mount not in GUEST_REPO_MOUNT_CHOICES:
            raise SandboxConfigError(
                f"unknown guest_repo_mount {self.guest_repo_mount!r}, "
                f"expected one of {GUEST_REPO_MOUNT_CHOICES}"
            )
        if self.rootfs_size is not None and not _ROOTFS_SIZE_RE.match(self.rootfs_size.strip()):
            raise SandboxConfigError(
                f"invalid rootfs_size {self.rootfs_size!r}; use a size like 4G or 512M"
            )
        self.host_loopback.validate()
        for secret in self.host_secrets:
            secret.validate()
        if self.host_loopback.enabled:
            routes = resolve_host_loopback_routes(self)
            if not routes:
                raise SandboxConfigError(
                    "host_loopback.enabled is true but no routes are configured"
                )


def repo_config_path(repo: Path) -> Path:
    return repo / MYAI_DIR / SANDBOX_CONFIG_FILE


def resolve_model_endpoint(cfg: SandboxConfig | None = None) -> str:
    if env := os.environ.get("MYAI_MODEL_ENDPOINT"):
        return env.strip()
    if cfg and cfg.model_endpoint:
        return cfg.model_endpoint
    return DEFAULT_MODEL_ENDPOINT


def resolve_host_loopback_enabled(cfg: SandboxConfig) -> bool:
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
        warm_reuse=data.get("warm_reuse", True),
        mount_readonly=data.get("mount_readonly", False),
        install_pi_at_boot=data.get("install_pi_at_boot", True),
        pi_package=data.get("pi_package", "@earendil-works/pi-coding-agent"),
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


def load_global_config() -> SandboxConfig | None:
    path = global_sandbox_config_path()
    if path is None or not path.is_file():
        legacy = sandbox_root() / SANDBOX_CONFIG_FILE
        if not legacy.is_file():
            return None
        path = legacy
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SandboxConfigError(f"invalid sandbox config at {path}: {exc}") from exc
    return _config_from_dict(data)


def load_repo_config(repo: Path) -> SandboxConfig | None:
    path = repo_config_path(repo)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SandboxConfigError(f"invalid sandbox config at {path}: {exc}") from exc
    return _config_from_dict(data)


def load_config(repo: Path | None = None) -> SandboxConfig:
    repo_cfg = load_repo_config(repo) if repo else None
    global_cfg = load_global_config()
    if repo_cfg and global_cfg:
        merged = _config_from_dict(_merge_dicts(_config_to_dict(global_cfg), _config_to_dict(repo_cfg)))
        return merged
    if repo_cfg:
        return repo_cfg
    if global_cfg:
        return global_cfg
    cfg = SandboxConfig()
    cfg.validate()
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
    path.write_text(json.dumps(_config_to_dict(cfg), indent=2) + "\n", encoding="utf-8")


def _config_to_dict(cfg: SandboxConfig) -> dict:
    return {
        "version": cfg.version,
        "host_loopback": {
            "enabled": cfg.host_loopback.enabled,
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
        "warm_reuse": cfg.warm_reuse,
        "mount_readonly": cfg.mount_readonly,
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
        if cfg.mirror_host_pi and ALPINE_MIRROR_HOST not in hosts:
            hosts.append(ALPINE_MIRROR_HOST)
    return hosts


def host_sessions_dir() -> Path:
    return Path.home() / ".pi" / "agent" / "sessions"


def effective_workspace_path(repo: Path, cfg: SandboxConfig) -> str:
    if cfg.guest_repo_mount == "workspace":
        return WORKSPACE_PATH
    return str(repo.resolve())


def gondolin_invocation(cfg: SandboxConfig) -> list[str]:
    pkg = cfg.gondolin_package
    if cfg.gondolin_version and cfg.gondolin_version != "latest":
        pkg = f"{pkg}@{cfg.gondolin_version}"
    return ["npx", "--yes", pkg]


def default_sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        version=CONFIG_VERSION,
        host_loopback=HostLoopbackConfig(enabled=False, routes=[]),
    )
