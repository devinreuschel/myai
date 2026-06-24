import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.agentsync.config import RepoConfig, save_config
from myai.agentsync.registry import set_master
from myai.sandbox.config import (
    DENY_ALL_SENTINEL,
    DEFAULT_MODEL_ENDPOINT,
    GUEST_AGENT_PATH,
    HostLoopbackConfig,
    HostLoopbackRoute,
    HostSecret,
    RouteProvision,
    SandboxConfig,
    SandboxConfigError,
    WORKSPACE_PATH,
    _config_from_dict,
    _config_to_dict,
    default_sandbox_config,
    effective_allow_hosts,
    effective_rootfs_size,
    effective_workspace_path,
    gondolin_package_spec,
    sidecar_invocation,
    host_sessions_dir,
    load_config,
    provision_allow_hosts,
    resolve_host_loopback_enabled,
    resolve_host_loopback_routes,
    resolve_model_endpoint,
    resolve_provider_domains,
    rewrite_endpoint_for_guest,
    runtime_allow_host_args,
    save_repo_config,
)
from myai.sandbox.doctor import doctor_ok, run_doctor
from myai.sandbox.gondolin import build_provision_plan, build_run_plan
from myai.sandbox.provision import (
    build_pi_launch_shell,
    build_provision_shell,
    cleanup_workspace_session_link,
    guest_agent_env,
    is_provisioned,
    needs_provision,
    pi_bin_dir,
    pi_install_dir,
    prepare_agent_dir,
    prepare_workspace_session_link,
    read_debug_missing_exes,
    render_guest_settings,
    render_models_json,
    session_dir_name,
)


def _write_master_rule(master: Path, name: str, body: str) -> None:
    """Write a minimal rule file under master/rules/."""
    rules_dir = master / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / f"{name}.md").write_text(body, encoding="utf-8")


class SandboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = self._tmp.name
        os.environ.pop("MYAI_HOST_LOOPBACK", None)
        os.environ.pop("MYAI_MODEL_ENDPOINT", None)

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home
        os.environ.pop("MYAI_HOST_LOOPBACK", None)
        os.environ.pop("MYAI_MODEL_ENDPOINT", None)
        self._tmp.cleanup()


def _loopback_cfg(**kwargs) -> SandboxConfig:
    cfg = SandboxConfig(**kwargs)
    cfg.host_loopback.enabled = True
    return cfg


class EndpointTests(SandboxTestCase):
    def test_resolve_env_override(self) -> None:
        os.environ["MYAI_MODEL_ENDPOINT"] = "http://127.0.0.1:9000/v1"
        self.assertEqual(resolve_model_endpoint(), "http://127.0.0.1:9000/v1")

    def test_resolve_config_default(self) -> None:
        cfg = SandboxConfig(model_endpoint="http://localhost:11434/v1")
        self.assertEqual(resolve_model_endpoint(cfg), "http://localhost:11434/v1")
        self.assertEqual(resolve_model_endpoint(None), DEFAULT_MODEL_ENDPOINT)

    def test_rewrite_localhost(self) -> None:
        guest = rewrite_endpoint_for_guest("http://localhost:8080/v1", "model.host")
        self.assertEqual(guest.guest_host, "model.host")
        self.assertEqual(guest.port, 8080)
        self.assertEqual(guest.guest_endpoint, "http://model.host:8080/v1")

    def test_rewrite_https_default_port(self) -> None:
        guest = rewrite_endpoint_for_guest("https://api.example.com/v1")
        self.assertEqual(guest.port, 443)
        self.assertEqual(guest.guest_endpoint, "https://model.host:443/v1")


class ConfigTests(SandboxTestCase):
    def test_invalid_vmm(self) -> None:
        cfg = SandboxConfig(vmm="docker")
        with self.assertRaises(SandboxConfigError):
            cfg.validate()

    def test_repo_config_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(model_endpoint="http://localhost:8080/v1")
            save_repo_config(repo, cfg)
            loaded = load_config(repo)
            self.assertEqual(loaded.model_endpoint, "http://localhost:8080/v1")
            self.assertFalse(loaded.host_loopback.enabled)

    def test_default_sandbox_config_cloud_first(self) -> None:
        cfg = default_sandbox_config()
        self.assertEqual(cfg.version, 2)
        self.assertFalse(cfg.host_loopback.enabled)
        self.assertEqual(cfg.host_loopback.routes, [])

    def test_default_share_host_sessions_and_repo_mount(self) -> None:
        cfg = default_sandbox_config()
        self.assertTrue(cfg.share_host_sessions)
        self.assertEqual(cfg.guest_repo_mount, "host_path")

    def test_invalid_guest_repo_mount(self) -> None:
        cfg = SandboxConfig(guest_repo_mount="bogus")
        with self.assertRaises(SandboxConfigError):
            cfg.validate()

    def test_session_config_roundtrip(self) -> None:
        cfg = SandboxConfig(
            share_host_sessions=False,
            guest_repo_mount="workspace",
        )
        loaded = _config_from_dict(_config_to_dict(cfg))
        self.assertFalse(loaded.share_host_sessions)
        self.assertEqual(loaded.guest_repo_mount, "workspace")

    def test_effective_workspace_path_host_path(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(guest_repo_mount="host_path")
            self.assertEqual(effective_workspace_path(repo, cfg), str(repo.resolve()))

    def test_effective_workspace_path_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(guest_repo_mount="workspace")
            self.assertEqual(effective_workspace_path(repo, cfg), WORKSPACE_PATH)

    def test_default_guest_hidden_paths(self) -> None:
        cfg = default_sandbox_config()
        self.assertEqual(cfg.guest_hidden_paths, ["/.myai"])

    def test_guest_hidden_paths_roundtrip(self) -> None:
        cfg = SandboxConfig(guest_hidden_paths=["/.myai", "/.env"])
        loaded = _config_from_dict(_config_to_dict(cfg))
        self.assertEqual(loaded.guest_hidden_paths, ["/.myai", "/.env"])

    def test_invalid_guest_hidden_path(self) -> None:
        with self.assertRaises(SandboxConfigError):
            SandboxConfig(guest_hidden_paths=[".myai"]).validate()

    def test_sidecar_invocation(self) -> None:
        cfg = SandboxConfig(gondolin_version="0.12.0")
        cmd = sidecar_invocation(cfg)
        self.assertEqual(cmd[0], "node")
        self.assertTrue(cmd[1].endswith("sidecar.mjs"))

    def test_gondolin_package_spec_pin(self) -> None:
        cfg = SandboxConfig(gondolin_version="0.12.0")
        self.assertEqual(gondolin_package_spec(cfg), "@earendil-works/gondolin@0.12.0")

    def test_enabled_without_routes_requires_legacy_synthesis(self) -> None:
        cfg = _loopback_cfg()
        routes = resolve_host_loopback_routes(cfg)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].route.id, "model")
        self.assertEqual(routes[0].guest.guest_endpoint, "http://model.host:8080/v1")

    def test_multi_route_resolution(self) -> None:
        cfg = _loopback_cfg(
            host_loopback=HostLoopbackConfig(
                enabled=True,
                routes=[
                    HostLoopbackRoute(
                        id="model",
                        guest_host="model.host",
                        upstream="http://localhost:8080/v1",
                        provision=RouteProvision(),
                    ),
                    HostLoopbackRoute(
                        id="mcp",
                        guest_host="mcp.host",
                        upstream="127.0.0.1:6277",
                    ),
                ],
            )
        )
        routes = resolve_host_loopback_routes(cfg)
        self.assertEqual(len(routes), 2)
        self.assertEqual(routes[1].guest.guest_host, "mcp.host")
        self.assertEqual(routes[1].upstream_port, 6277)

    def test_two_provision_routes_invalid(self) -> None:
        cfg = SandboxConfig(
            host_loopback=HostLoopbackConfig(
                enabled=True,
                routes=[
                    HostLoopbackRoute(
                        id="a",
                        guest_host="a.host",
                        upstream="http://localhost:8080/v1",
                        provision=RouteProvision(provider="p1", model_id="m1"),
                    ),
                    HostLoopbackRoute(
                        id="b",
                        guest_host="b.host",
                        upstream="http://localhost:9000/v1",
                        provision=RouteProvision(provider="p2", model_id="m2"),
                    ),
                ],
            )
        )
        with self.assertRaises(SandboxConfigError):
            cfg.validate()

    def test_global_config_from_tilde_myai(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            global_path = home / ".myai" / "sandbox.json"
            global_path.parent.mkdir(parents=True)
            global_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "host_loopback": {"enabled": True, "routes": []},
                        "allow_hosts": ["api.github.com"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("myai.sandbox.config.global_sandbox_config_path", return_value=global_path):
                with patch("myai.paths.global_myai_dir", return_value=home / ".myai"):
                    loaded = load_config()
            self.assertTrue(loaded.host_loopback.enabled)
            self.assertEqual(loaded.allow_hosts, ["api.github.com"])

    def test_repo_replaces_host_loopback_wholesale(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            home = Path(tmp) / "home"
            global_path = home / ".myai" / "sandbox.json"
            global_path.parent.mkdir(parents=True)
            global_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "host_loopback": {
                            "enabled": True,
                            "routes": [
                                {
                                    "id": "model",
                                    "guest_host": "model.host",
                                    "upstream": "http://localhost:8080/v1",
                                }
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            save_repo_config(
                repo,
                SandboxConfig(
                    host_loopback=HostLoopbackConfig(enabled=False, routes=[]),
                ),
            )
            with patch("myai.sandbox.config.global_sandbox_config_path", return_value=global_path):
                loaded = load_config(repo)
            self.assertFalse(loaded.host_loopback.enabled)
            self.assertEqual(loaded.host_loopback.routes, [])

    def test_env_host_loopback_override(self) -> None:
        cfg = SandboxConfig(host_loopback=HostLoopbackConfig(enabled=False))
        os.environ["MYAI_HOST_LOOPBACK"] = "1"
        self.assertTrue(resolve_host_loopback_enabled(cfg))
        os.environ["MYAI_HOST_LOOPBACK"] = "0"
        self.assertFalse(resolve_host_loopback_enabled(cfg))


class ProvisionTests(SandboxTestCase):
    def test_models_json_empty_when_loopback_disabled(self) -> None:
        cfg = SandboxConfig()
        self.assertEqual(render_models_json(cfg), "{}\n")

    def test_models_json_contains_guest_endpoint_when_enabled(self) -> None:
        cfg = _loopback_cfg()
        text = render_models_json(cfg)
        data = json.loads(text)
        self.assertEqual(
            data["providers"]["myai-local"]["baseUrl"],
            "http://model.host:8080/v1",
        )

    def test_prepare_agent_dir_writes_empty_models_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig()
            staging = prepare_agent_dir(repo, cfg)
            models = staging / "models.json"
            self.assertTrue(models.is_file())
            self.assertEqual(json.loads(models.read_text()), {})

    def test_prepare_agent_dir_writes_models_json_when_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = _loopback_cfg()
            staging = prepare_agent_dir(repo, cfg)
            models = staging / "models.json"
            self.assertTrue(models.is_file())
            payload = json.loads(models.read_text())
            self.assertIn("myai-local", payload["providers"])

    def test_pi_launch_skips_provider_when_loopback_disabled(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=False)
        _, args = build_pi_launch_shell(cfg, ["-p", "hello"], WORKSPACE_PATH)
        self.assertNotIn("--provider", args)
        self.assertNotIn("--model", args)

    def test_pi_launch_uses_cached_pi_binary(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
        _, args = build_pi_launch_shell(cfg, ["-p", "hello"], WORKSPACE_PATH)
        script = args[1]
        self.assertIn('PI_BIN="/opt/pi/node_modules/.bin/pi"', script)
        self.assertIn('exec "$PI_BIN"', script)
        self.assertNotIn("npm install", script)

    def test_provision_shell_installs_pi_and_tools(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
        _, args = build_provision_shell(cfg)
        script = args[1]
        self.assertIn("PI_PREFIX=/opt/pi", script)
        self.assertIn('npm install --prefix "$PI_PREFIX"', script)
        self.assertIn("tools-manager.js", script)
        self.assertIn("ensureTool('fd')", script)
        self.assertIn("ensureTool('rg')", script)

    def test_provision_shell_installs_git_when_mirroring(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest", mirror_host_pi=True)
        _, args = build_provision_shell(cfg)
        script = args[1]
        self.assertIn("apk add --no-cache git", script)
        self.assertIn("update --extensions", script)

    def test_pi_launch_skips_provider_when_mirroring(self) -> None:
        cfg = _loopback_cfg(install_pi_at_boot=False, mirror_host_pi=True)
        _, args = build_pi_launch_shell(cfg, ["-p", "hello"], WORKSPACE_PATH)
        self.assertNotIn("--provider", args)
        self.assertNotIn("--model", args)

    def test_guest_env_no_llama_url_by_default(self) -> None:
        cfg = SandboxConfig()
        env = guest_agent_env(cfg)
        self.assertIn("PI_CODING_AGENT_DIR=/root/.pi/agent", env)
        self.assertFalse(any(e.startswith("LLAMA_SERVER_URL=") for e in env))

    def test_guest_env_forwards_term(self) -> None:
        env = guest_agent_env(SandboxConfig())
        self.assertTrue(any(e.startswith("TERM=") for e in env))
        self.assertFalse(any(e == "TERM=" for e in env))

    def test_guest_env_passes_llama_url(self) -> None:
        cfg = SandboxConfig(llama_server_url="http://model.host:8080")
        env = guest_agent_env(cfg)
        self.assertIn("LLAMA_SERVER_URL=http://model.host:8080", env)

    def test_guest_env_rewrites_localhost_llama_url(self) -> None:
        cfg = _loopback_cfg(llama_server_url="http://127.0.0.1:8080")
        env = guest_agent_env(cfg)
        self.assertIn("LLAMA_SERVER_URL=http://model.host:8080", env)

    def test_render_guest_settings_none_when_disabled(self) -> None:
        cfg = SandboxConfig(mirror_host_pi=False)
        self.assertIsNone(render_guest_settings(cfg))

    def test_render_guest_settings_mirrors_and_rewrites(self) -> None:
        with TemporaryDirectory() as tmp:
            host_settings = Path(tmp) / "settings.json"
            host_settings.write_text(
                json.dumps(
                    {
                        "packages": ["https://github.com/foo/pi-llama-cpp"],
                        "defaultProvider": "llama-server=http://127.0.0.1:8080",
                        "defaultModel": "unsloth/Qwen3",
                        "auth": "should-not-leak",
                    }
                )
            )
            cfg = _loopback_cfg(mirror_host_pi=True)
            with patch(
                "myai.sandbox.provision.host_pi_settings_path",
                return_value=host_settings,
            ):
                out = json.loads(render_guest_settings(cfg))
            self.assertEqual(out["packages"], ["https://github.com/foo/pi-llama-cpp"])
            self.assertEqual(out["defaultProvider"], "llama-server=http://model.host:8080")
            self.assertEqual(out["defaultModel"], "unsloth/Qwen3")
            self.assertNotIn("auth", out)

    def test_prepare_agent_dir_writes_settings_when_mirroring(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            host_settings = Path(tmp) / "settings.json"
            host_settings.write_text(json.dumps({"defaultModel": "unsloth/Qwen3"}))
            cfg = _loopback_cfg(mirror_host_pi=True)
            with patch(
                "myai.sandbox.provision.host_pi_settings_path",
                return_value=host_settings,
            ):
                staging = prepare_agent_dir(repo, cfg)
            settings = staging / "settings.json"
            self.assertTrue(settings.is_file())
            self.assertEqual(json.loads(settings.read_text())["defaultModel"], "unsloth/Qwen3")

    def test_prepare_agent_dir_creates_sessions_mountpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            staging = prepare_agent_dir(repo, SandboxConfig())
            self.assertTrue((staging / "sessions").is_dir())

    def test_prepare_agent_dir_skips_agents_md_when_unmanaged(self) -> None:
        with TemporaryDirectory() as tmp:
            master = Path(tmp) / "master"
            master.mkdir()
            _write_master_rule(master, "general", "General rule body for sandbox test\n")
            set_master(master)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            staging = prepare_agent_dir(repo, SandboxConfig())
            self.assertFalse((staging / "AGENTS.md").exists())

    def test_prepare_agent_dir_skips_agents_md_when_pi_managed(self) -> None:
        with TemporaryDirectory() as tmp:
            master = Path(tmp) / "master"
            master.mkdir()
            _write_master_rule(master, "general", "General rule body for sandbox test\n")
            set_master(master)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            save_config(
                repo,
                RepoConfig(agents=["cursor", "pi"], rules=["general"]),
            )
            staging = prepare_agent_dir(repo, SandboxConfig())
            self.assertFalse((staging / "AGENTS.md").exists())

    def test_prepare_agent_dir_writes_agents_md_for_managed_non_pi(self) -> None:
        with TemporaryDirectory() as tmp:
            master = Path(tmp) / "master"
            master.mkdir()
            _write_master_rule(master, "general", "General rule body for sandbox test\n")
            set_master(master)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            save_config(
                repo,
                RepoConfig(agents=["cursor"], rules=["general"]),
            )
            staging = prepare_agent_dir(repo, SandboxConfig())
            agents_md = staging / "AGENTS.md"
            self.assertTrue(agents_md.is_file())
            text = agents_md.read_text(encoding="utf-8")
            self.assertIn("# Project rules (myai)", text)
            self.assertIn("General rule body for sandbox test", text)

    def test_pi_launch_uses_workspace_path_in_cd(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ws = str(repo.resolve())
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
            _, args = build_pi_launch_shell(cfg, [], ws)
            self.assertIn(f"cd {ws}", args[1])

    def test_pi_launch_injects_provider_when_loopback_enabled(self) -> None:
        cfg = _loopback_cfg(install_pi_at_boot=False)
        _, args = build_pi_launch_shell(cfg, ["-p", "hello"], WORKSPACE_PATH)
        self.assertIn("--provider", args)
        self.assertIn("myai-local", args)
        self.assertIn("--model", args)
        self.assertIn("local", args)


class VmSpecTests(SandboxTestCase):
    def _plan_spec(self, repo: Path, cfg: SandboxConfig, pi_args: list[str] | None = None):
        plan = build_run_plan(repo, cfg, pi_args or [])
        try:
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            return plan, data
        finally:
            plan.spec_path.unlink(missing_ok=True)

    def _provision_spec(self, repo: Path, cfg: SandboxConfig):
        plan = build_provision_plan(repo, cfg)
        try:
            return plan, json.loads(plan.spec_path.read_text(encoding="utf-8"))
        finally:
            plan.spec_path.unlink(missing_ok=True)

    def test_build_run_plan_sidecar_argv(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False)
            plan, _ = self._plan_spec(repo, cfg, ["-p", "hello"])
            self.assertEqual(plan.cmd[0], "node")
            self.assertTrue(plan.cmd[1].endswith("sidecar.mjs"))
            self.assertEqual(plan.mode, "run")

    def test_build_run_spec_hides_myai(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False)
            _, data = self._plan_spec(repo, cfg, [])
            self.assertIn("/.myai", data["vfs"]["workspace"]["hiddenPaths"])

    def test_build_run_spec_no_tcp_map_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False)
            _, data = self._plan_spec(repo, cfg, ["-p", "hello"])
            self.assertEqual(data["cwd"], str(repo.resolve()))
            self.assertEqual(data["network"]["tcpHosts"], {})
            self.assertEqual(data["command"][0], "pi")
            self.assertIn("-a", data["command"])

    def test_build_run_spec_legacy_loopback(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = _loopback_cfg(install_pi_at_boot=False)
            _, data = self._plan_spec(repo, cfg, ["-p", "hello"])
            self.assertEqual(
                data["network"]["tcpHosts"],
                {"model.host:8080": "127.0.0.1:8080"},
            )
            self.assertEqual(data["env"]["PI_CODING_AGENT_DIR"], GUEST_AGENT_PATH)
            self.assertIn("--provider", data["command"])

    def test_build_run_spec_multi_route(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = _loopback_cfg(
                install_pi_at_boot=False,
                host_loopback=HostLoopbackConfig(
                    enabled=True,
                    routes=[
                        HostLoopbackRoute(
                            id="model",
                            guest_host="model.host",
                            upstream="http://localhost:8080/v1",
                            provision=RouteProvision(),
                        ),
                        HostLoopbackRoute(
                            id="mcp",
                            guest_host="mcp.host",
                            upstream="127.0.0.1:6277",
                        ),
                    ],
                ),
            )
            _, data = self._plan_spec(repo, cfg, [])
            self.assertEqual(
                data["network"]["tcpHosts"],
                {
                    "model.host:8080": "127.0.0.1:8080",
                    "mcp.host:6277": "127.0.0.1:6277",
                },
            )

    def test_host_secret_in_spec_not_argv(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(
                install_pi_at_boot=False,
                host_secrets=[HostSecret(name="OPENAI_API_KEY", hosts=["api.openai.com"])],
            )
            os.environ["OPENAI_API_KEY"] = "sk-test"
            try:
                plan, data = self._plan_spec(repo, cfg, [])
            finally:
                os.environ.pop("OPENAI_API_KEY", None)
            self.assertIn("OPENAI_API_KEY", data["network"]["secrets"])
            self.assertNotIn("sk-test", " ".join(plan.cmd))
            self.assertEqual(plan.env["OPENAI_API_KEY"], "sk-test")

    def test_effective_allow_hosts_disabled(self) -> None:
        cfg = SandboxConfig(allow_hosts=["api.github.com"], install_pi_at_boot=False)
        hosts = effective_allow_hosts(cfg)
        self.assertEqual(hosts, ["api.github.com"])

    def test_effective_allow_hosts_enabled(self) -> None:
        cfg = _loopback_cfg(allow_hosts=["api.github.com"])
        hosts = effective_allow_hosts(cfg)
        self.assertIn("model.host", hosts)
        self.assertIn("model.host:8080", hosts)
        self.assertIn("api.github.com", hosts)

    def test_effective_allow_hosts_runtime_excludes_install_hosts(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
        hosts = effective_allow_hosts(cfg)
        self.assertEqual(hosts, [])

    def test_provision_allow_hosts_includes_install_hosts(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
        hosts = provision_allow_hosts(cfg)
        self.assertIn("registry.npmjs.org", hosts)
        self.assertIn("api.github.com", hosts)
        self.assertIn("github.com", hosts)
        self.assertIn("release-assets.githubusercontent.com", hosts)

    def test_provision_allow_hosts_includes_alpine_when_mirroring(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest", mirror_host_pi=True)
        hosts = provision_allow_hosts(cfg)
        self.assertIn("dl-cdn.alpinelinux.org", hosts)

    def test_effective_rootfs_size_only_when_explicit(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
        self.assertIsNone(effective_rootfs_size(cfg))

    def test_effective_rootfs_size_explicit_override(self) -> None:
        cfg = SandboxConfig(rootfs_size="8G", install_pi_at_boot=False)
        self.assertEqual(effective_rootfs_size(cfg), "8G")

    def test_build_run_spec_rootfs_size_when_explicit(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(rootfs_size="4G", install_pi_at_boot=True, image="alpine-base:latest")
            _, data = self._plan_spec(repo, cfg, [])
            self.assertEqual(data["rootfsSize"], "4G")

    def test_build_run_spec_pi_install_mount(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
            _, data = self._plan_spec(repo, cfg, [])
            guest_paths = [m["guestPath"] for m in data["vfs"]["mounts"]]
            self.assertIn("/opt/pi", guest_paths)
            self.assertIn(f"{GUEST_AGENT_PATH}/bin", guest_paths)
            self.assertIsNone(data["rootfsSize"])

    def test_build_run_spec_runtime_no_github_allow(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
            _, data = self._plan_spec(repo, cfg, [])
            allow = data["network"]["allowedHosts"]
            self.assertNotIn("github.com", allow)
            self.assertNotIn("registry.npmjs.org", allow)

    def test_build_provision_spec_allows_github(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
            _, data = self._provision_spec(repo, cfg)
            allow = data["network"]["allowedHosts"]
            self.assertIn("github.com", allow)
            self.assertIn("registry.npmjs.org", allow)
            self.assertEqual(data["network"]["tcpHosts"], {})

    def test_build_run_spec_mirror_pkg_mounts(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest", mirror_host_pi=True)
            _, data = self._plan_spec(repo, cfg, [])
            guest_paths = [m["guestPath"] for m in data["vfs"]["mounts"]]
            self.assertIn(f"{GUEST_AGENT_PATH}/npm", guest_paths)
            self.assertIn(f"{GUEST_AGENT_PATH}/git", guest_paths)

    def test_build_run_spec_no_rootfs_size_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False, image="alpine-base:latest")
            _, data = self._plan_spec(repo, cfg, [])
            self.assertIsNone(data["rootfsSize"])

    def test_build_run_spec_workspace_mount_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(guest_repo_mount="workspace", install_pi_at_boot=False)
            _, data = self._plan_spec(repo, cfg, [])
            self.assertEqual(data["vfs"]["workspace"]["guestPath"], WORKSPACE_PATH)
            self.assertEqual(data["cwd"], WORKSPACE_PATH)

    def test_build_run_spec_sessions_mount_when_shared(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(share_host_sessions=True, install_pi_at_boot=False)
            _, data = self._plan_spec(repo, cfg, [])
            guest_paths = [m["guestPath"] for m in data["vfs"]["mounts"]]
            self.assertIn(f"{GUEST_AGENT_PATH}/sessions", guest_paths)
            sessions_mount = next(
                m for m in data["vfs"]["mounts"] if m["guestPath"] == f"{GUEST_AGENT_PATH}/sessions"
            )
            self.assertEqual(
                sessions_mount["hostPath"],
                str(host_sessions_dir().resolve()),
            )

    def test_build_run_spec_no_sessions_mount_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(share_host_sessions=False, install_pi_at_boot=False)
            _, data = self._plan_spec(repo, cfg, [])
            guest_paths = [m["guestPath"] for m in data["vfs"]["mounts"]]
            self.assertNotIn(f"{GUEST_AGENT_PATH}/sessions", guest_paths)

    def test_prepare_agent_dir_sessions_is_real_dir_not_symlink(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(share_host_sessions=True, install_pi_at_boot=False)
            staging = prepare_agent_dir(repo, cfg)
            sessions = staging / "sessions"
            self.assertTrue(sessions.is_dir())
            self.assertFalse(sessions.is_symlink())


class ProvisionStateTests(SandboxTestCase):
    def test_needs_provision_when_empty(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
        self.assertTrue(needs_provision(cfg))

    def test_is_provisioned_when_cached(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
        pi_bin = pi_install_dir() / "node_modules" / ".bin" / "pi"
        pi_bin.parent.mkdir(parents=True, exist_ok=True)
        pi_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        for tool in ("fd", "rg"):
            path = pi_bin_dir() / tool
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        self.assertTrue(is_provisioned(cfg))
        self.assertFalse(needs_provision(cfg))

    def test_needs_provision_skipped_for_custom_image(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=False)
        self.assertFalse(needs_provision(cfg))


class CliOverrideTests(SandboxTestCase):
    def test_cfg_from_args_model_endpoint_enables_loopback(self) -> None:
        from argparse import Namespace

        from myai.commands.sandbox import _cfg_from_args

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            args = Namespace(
                model_endpoint="http://localhost:9000/v1",
                allow_hosts=[],
                providers=[],
                network_policy=None,
                vmm=None,
                image=None,
                rootfs_size=None,
                ro=False,
                guest_hidden_paths=[],
                host_loopback=False,
                no_host_loopback=False,
                no_auto_approve=False,
                mirror_host_pi=False,
            )
            cfg = _cfg_from_args(repo, args)
            self.assertTrue(cfg.host_loopback.enabled)
            self.assertEqual(cfg.model_endpoint, "http://localhost:9000/v1")

    def test_cfg_from_args_no_host_loopback(self) -> None:
        from argparse import Namespace

        from myai.commands.sandbox import _cfg_from_args

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            save_repo_config(repo, _loopback_cfg())
            args = Namespace(
                model_endpoint=None,
                allow_hosts=[],
                providers=[],
                network_policy=None,
                vmm=None,
                image=None,
                rootfs_size=None,
                ro=False,
                guest_hidden_paths=[],
                host_loopback=False,
                no_host_loopback=True,
                no_auto_approve=False,
                mirror_host_pi=False,
            )
            cfg = _cfg_from_args(repo, args)
            self.assertFalse(cfg.host_loopback.enabled)


class DoctorTests(unittest.TestCase):
    def test_run_doctor_returns_checks(self) -> None:
        results = run_doctor()
        names = {r.name for r in results}
        self.assertIn("node", names)
        self.assertIn("npm", names)
        self.assertIn("qemu", names)
        self.assertIn("sidecar", names)
        self.assertIn("disk", names)

    def test_doctor_ok_requires_core(self) -> None:
        from myai.sandbox.doctor import CheckResult

        ok = [
            CheckResult("node", True, ""),
            CheckResult("npm", True, ""),
            CheckResult("qemu", True, ""),
            CheckResult("virtualization", True, ""),
            CheckResult("disk", True, ""),
            CheckResult("krun", False, "", "optional"),
        ]
        self.assertTrue(doctor_ok(ok))


class NetworkPolicyTests(SandboxTestCase):
    def test_default_policy_is_custom(self) -> None:
        self.assertEqual(SandboxConfig().network_policy, "custom")

    def test_invalid_network_policy(self) -> None:
        with self.assertRaises(SandboxConfigError):
            SandboxConfig(network_policy="open").validate()

    def test_custom_empty_collapses_to_sentinel_not_unrestricted(self) -> None:
        hosts, unrestricted = runtime_allow_host_args(SandboxConfig())
        self.assertEqual(hosts, [DENY_ALL_SENTINEL])
        self.assertFalse(unrestricted)

    def test_deny_all_passes_sentinel_only(self) -> None:
        cfg = SandboxConfig(network_policy="deny-all", allow_hosts=["api.openai.com"])
        hosts, unrestricted = runtime_allow_host_args(cfg)
        self.assertEqual(hosts, [DENY_ALL_SENTINEL])
        self.assertFalse(unrestricted)

    def test_allow_all_is_unrestricted_with_no_flags(self) -> None:
        hosts, unrestricted = runtime_allow_host_args(SandboxConfig(network_policy="allow-all"))
        self.assertEqual(hosts, [])
        self.assertTrue(unrestricted)

    def test_custom_with_hosts_passes_hosts(self) -> None:
        cfg = SandboxConfig(allow_hosts=["api.openai.com"], install_pi_at_boot=False)
        hosts, unrestricted = runtime_allow_host_args(cfg)
        self.assertEqual(hosts, ["api.openai.com"])
        self.assertFalse(unrestricted)

    def test_build_run_spec_fails_closed_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False)
            plan = build_run_plan(repo, cfg, [])
            try:
                data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            finally:
                plan.spec_path.unlink(missing_ok=True)
            self.assertEqual(data["network"]["allowedHosts"], [DENY_ALL_SENTINEL])

    def test_build_run_spec_allow_all_policy(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False, network_policy="allow-all")
            plan = build_run_plan(repo, cfg, [])
            try:
                data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            finally:
                plan.spec_path.unlink(missing_ok=True)
            self.assertEqual(data["network"]["policy"], "allow-all")


class ProviderPresetTests(SandboxTestCase):
    def test_resolve_known_provider(self) -> None:
        cfg = SandboxConfig(providers=["anthropic"])
        self.assertEqual(resolve_provider_domains(cfg), ["api.anthropic.com"])

    def test_unknown_provider_rejected(self) -> None:
        with self.assertRaises(SandboxConfigError):
            SandboxConfig(providers=["bogus"]).validate()

    def test_providers_merged_into_allow_hosts(self) -> None:
        cfg = SandboxConfig(providers=["anthropic"], allow_hosts=["x.example.com"], install_pi_at_boot=False)
        hosts = effective_allow_hosts(cfg)
        self.assertIn("api.anthropic.com", hosts)
        self.assertIn("x.example.com", hosts)

    def test_provider_domains_reach_run_spec(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(providers=["openai"], install_pi_at_boot=False)
            plan = build_run_plan(repo, cfg, [])
            try:
                data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            finally:
                plan.spec_path.unlink(missing_ok=True)
            allow = data["network"]["allowedHosts"]
            self.assertIn("api.openai.com", allow)
            self.assertNotIn(DENY_ALL_SENTINEL, allow)


class SecretEnvTests(SandboxTestCase):
    def test_env_var_rename_maps_value(self) -> None:
        from myai.sandbox.gondolin import secret_child_env

        cfg = SandboxConfig(host_secrets=[HostSecret(name="GH_TOKEN", hosts=["api.github.com"], env_var="MY_PAT")])
        env, missing = secret_child_env(cfg, {"MY_PAT": "abc"})
        self.assertEqual(env["GH_TOKEN"], "abc")
        self.assertEqual(missing, [])

    def test_missing_secret_reported(self) -> None:
        from myai.sandbox.gondolin import secret_child_env

        cfg = SandboxConfig(host_secrets=[HostSecret(name="GH_TOKEN", hosts=["api.github.com"])])
        env, missing = secret_child_env(cfg, {})
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(missing, ["GH_TOKEN"])

    def test_secret_value_not_on_argv(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(
                install_pi_at_boot=False,
                host_secrets=[HostSecret(name="GH_TOKEN", hosts=["api.github.com"], env_var="MY_PAT")],
            )
            os.environ["MY_PAT"] = "secret-value"
            try:
                plan = build_run_plan(repo, cfg, [])
                data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            finally:
                os.environ.pop("MY_PAT", None)
                plan.spec_path.unlink(missing_ok=True)
            self.assertNotIn("secret-value", " ".join(plan.cmd))
            self.assertIn("GH_TOKEN", data["network"]["secrets"])
            self.assertEqual(plan.env["GH_TOKEN"], "secret-value")


class AutoApproveTests(SandboxTestCase):
    def test_auto_approve_injects_a(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=False)
        _, args = build_pi_launch_shell(cfg, ["-p", "hi"], WORKSPACE_PATH)
        self.assertIn("-a", args)

    def test_no_auto_approve_omits_a(self) -> None:
        cfg = SandboxConfig(install_pi_at_boot=False, auto_approve=False)
        _, args = build_pi_launch_shell(cfg, ["-p", "hi"], WORKSPACE_PATH)
        self.assertNotIn("-a", args)


class WorkspaceSessionLinkTests(SandboxTestCase):
    def test_session_dir_name_encoding(self) -> None:
        self.assertEqual(session_dir_name("/workspace"), "--workspace--")
        self.assertEqual(session_dir_name("/home/a/proj"), "--home-a-proj--")

    def test_no_link_for_host_path_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(guest_repo_mount="host_path")
            self.assertIsNone(prepare_workspace_session_link(repo, cfg))

    def test_workspace_mode_links_to_repo_session_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sessions = Path(tmp) / "sessions"
            cfg = SandboxConfig(guest_repo_mount="workspace", share_host_sessions=True)
            with patch("myai.sandbox.provision.host_sessions_dir", return_value=sessions):
                link = prepare_workspace_session_link(repo, cfg)
                self.assertIsNotNone(link)
                assert link is not None
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.name, "--workspace--")
                self.assertEqual(
                    link.resolve(),
                    (sessions / session_dir_name(str(repo.resolve()))).resolve(),
                )
                cleanup_workspace_session_link(link)
                self.assertFalse(link.exists())

    def test_no_link_when_sessions_not_shared(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(guest_repo_mount="workspace", share_host_sessions=False)
            self.assertIsNone(prepare_workspace_session_link(repo, cfg))


class DebugAuditTests(SandboxTestCase):
    def test_prepare_agent_dir_writes_debug_init(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            staging = prepare_agent_dir(repo, SandboxConfig(), debug=True)
            init = staging / ".debug" / "init.sh"
            self.assertTrue(init.is_file())
            self.assertIn("command_not_found_handle", init.read_text())

    def test_no_debug_dir_without_debug(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            staging = prepare_agent_dir(repo, SandboxConfig())
            self.assertFalse((staging / ".debug").exists())

    def test_guest_env_sets_bash_env_in_debug(self) -> None:
        env = guest_agent_env(SandboxConfig(), debug=True)
        self.assertTrue(any(e.startswith("BASH_ENV=") for e in env))
        self.assertFalse(any(e.startswith("BASH_ENV=") for e in guest_agent_env(SandboxConfig())))

    def test_read_debug_missing_exes(self) -> None:
        with TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / ".debug").mkdir()
            (staging / ".debug" / "missing-exes.log").write_text("cargo\nrustc\ncargo\n")
            self.assertEqual(read_debug_missing_exes(staging), ["cargo", "rustc"])

    def test_read_debug_missing_exes_empty_when_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(read_debug_missing_exes(Path(tmp)), [])


class ConfigRoundtripNewFieldsTests(SandboxTestCase):
    def test_new_fields_roundtrip(self) -> None:
        cfg = SandboxConfig(
            network_policy="deny-all",
            providers=["anthropic", "github"],
            auto_approve=False,
        )
        loaded = _config_from_dict(_config_to_dict(cfg))
        self.assertEqual(loaded.network_policy, "deny-all")
        self.assertEqual(loaded.providers, ["anthropic", "github"])
        self.assertFalse(loaded.auto_approve)


if __name__ == "__main__":
    unittest.main()
