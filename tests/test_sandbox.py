import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.agentsync.config import RepoConfig, save_config
from myai.agentsync.registry import set_master
from myai.agentsync.render import MYAI_MANAGED_RULE
from myai.global_config import set_inject_myai_rule_default
from myai.sandbox.trust import (
    UntrustedConfigError,
    describe_grants,
    is_trusted,
    load_trusted_config,
    revoke_repo,
    trust_repo,
)
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
    ALPINE_MIRROR_HOST,
    effective_allow_hosts,
    effective_hidden_paths,
    effective_rootfs_size,
    effective_workspace_path,
    gondolin_package_spec,
    sidecar_invocation,
    load_config,
    provision_allow_hosts,
    resolve_host_loopback_enabled,
    resolve_host_loopback_routes,
    resolve_model_endpoint,
    resolve_provider_domains,
    rewrite_endpoint_for_guest,
    runtime_allow_host_args,
    save_repo_config,
    validate_host_pattern,
)
from myai.sandbox.doctor import doctor_ok, run_doctor
from myai.sandbox.gondolin import (
    GondolinError,
    build_provision_plan,
    build_run_plan,
    check_mountable,
)
from myai.sandbox.provision import (
    build_pi_launch_shell,
    build_provision_shell,
    guest_agent_env,
    is_provisioned,
    needs_provision,
    pi_bin_dir,
    pi_install_dir,
    build_git_bundle_shell,
    git_bundle_dir,
    prepare_agent_dir,
    read_debug_missing_exes,
    remove_stale_workspace_link,
    render_guest_settings,
    render_models_json,
    scrub_session_symlinks,
    session_dir_name,
    session_slot_mount,
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
        # building a spec creates the repo's session slot; keep that out of the
        # real ~/.pi/agent/sessions
        self.sessions = Path(self._tmp.name) / "pi-sessions"
        self._sessions_patch = patch(
            "myai.sandbox.provision.host_sessions_dir", return_value=self.sessions
        )
        self._sessions_patch.start()
        # ...and the user's real ~/.myai/sandbox.json out of every config load
        self.global_dir = Path(self._tmp.name) / "dot-myai"
        self.global_dir.mkdir()
        self._global_patch = patch("myai.paths.global_myai_dir", return_value=self.global_dir)
        self._global_patch.start()

    def write_global(self, data: dict) -> None:
        (self.global_dir / "sandbox.json").write_text(json.dumps(data), encoding="utf-8")

    def tearDown(self) -> None:
        self._global_patch.stop()
        self._sessions_patch.stop()
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

    def test_prepare_agent_dir_writes_append_system_for_managed_non_pi(self) -> None:
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
            append = staging / "APPEND_SYSTEM.md"
            self.assertTrue(append.is_file())
            self.assertEqual(append.read_text(encoding="utf-8"), MYAI_MANAGED_RULE)

    def test_prepare_agent_dir_skips_append_system_when_pi_managed(self) -> None:
        with TemporaryDirectory() as tmp:
            master = Path(tmp) / "master"
            master.mkdir()
            set_master(master)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            save_config(
                repo,
                RepoConfig(agents=["cursor", "pi"], rules=["general"]),
            )
            staging = prepare_agent_dir(repo, SandboxConfig())
            self.assertFalse((staging / "APPEND_SYSTEM.md").exists())

    def test_prepare_agent_dir_skips_append_system_when_unmanaged(self) -> None:
        with TemporaryDirectory() as tmp:
            master = Path(tmp) / "master"
            master.mkdir()
            set_master(master)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            staging = prepare_agent_dir(repo, SandboxConfig())
            self.assertFalse((staging / "APPEND_SYSTEM.md").exists())

    def test_prepare_agent_dir_skips_append_system_when_toggle_off(self) -> None:
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".myai"
            config_dir.mkdir()
            old_home = os.environ.get("MYAI_HOME")
            os.environ["MYAI_HOME"] = tmp
            try:
                with patch("myai.paths.global_myai_dir", return_value=config_dir):
                    master = Path(tmp) / "master"
                    master.mkdir()
                    _write_master_rule(master, "general", "General rule body for sandbox test\n")
                    set_master(master)
                    set_inject_myai_rule_default(False)
                    repo = Path(tmp) / "repo"
                    repo.mkdir()
                    save_config(
                        repo,
                        RepoConfig(agents=["cursor"], rules=["general"]),
                    )
                    staging = prepare_agent_dir(repo, SandboxConfig())
                    self.assertFalse((staging / "APPEND_SYSTEM.md").exists())
            finally:
                if old_home is None:
                    os.environ.pop("MYAI_HOME", None)
                else:
                    os.environ["MYAI_HOME"] = old_home

    def test_prepare_agent_dir_respects_per_repo_override_off(self) -> None:
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".myai"
            config_dir.mkdir()
            old_home = os.environ.get("MYAI_HOME")
            os.environ["MYAI_HOME"] = tmp
            try:
                with patch("myai.paths.global_myai_dir", return_value=config_dir):
                    master = Path(tmp) / "master"
                    master.mkdir()
                    set_master(master)
                    set_inject_myai_rule_default(True)
                    repo = Path(tmp) / "repo"
                    repo.mkdir()
                    save_config(
                        repo,
                        RepoConfig(agents=["cursor"], rules=["general"], inject_myai_rule=False),
                    )
                    staging = prepare_agent_dir(repo, SandboxConfig())
                    self.assertFalse((staging / "APPEND_SYSTEM.md").exists())
            finally:
                if old_home is None:
                    os.environ.pop("MYAI_HOME", None)
                else:
                    os.environ["MYAI_HOME"] = old_home

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
            slot = session_dir_name(str(repo.resolve()))
            mounts = {m["guestPath"]: m for m in data["vfs"]["mounts"]}
            # only this repo's slot, never the tree holding every project's transcripts
            self.assertNotIn(f"{GUEST_AGENT_PATH}/sessions", mounts)
            self.assertEqual(
                mounts[f"{GUEST_AGENT_PATH}/sessions/{slot}"]["hostPath"],
                str((self.sessions / slot).resolve()),
            )

    def test_build_run_spec_workspace_mode_mounts_repo_slot_as_workspace_slot(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(
                share_host_sessions=True, guest_repo_mount="workspace", install_pi_at_boot=False
            )
            _, data = self._plan_spec(repo, cfg, [])
            mounts = {m["guestPath"]: m for m in data["vfs"]["mounts"]}
            self.assertEqual(
                mounts[f"{GUEST_AGENT_PATH}/sessions/--workspace--"]["hostPath"],
                str((self.sessions / session_dir_name(str(repo.resolve()))).resolve()),
            )

    def test_build_provision_spec_has_no_sessions_mount(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(share_host_sessions=True)
            plan = build_provision_plan(repo, cfg)
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            plan.spec_path.unlink()
            for mount in data["vfs"]["mounts"]:
                self.assertNotIn("/sessions", mount["guestPath"])

    def test_build_run_spec_no_sessions_mount_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(share_host_sessions=False, install_pi_at_boot=False)
            _, data = self._plan_spec(repo, cfg, [])
            for mount in data["vfs"]["mounts"]:
                self.assertNotIn("/sessions", mount["guestPath"])

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
        self.assertFalse(is_provisioned(cfg), "git bundle missing -> not provisioned")
        (pi_bin_dir() / "git").write_text("", encoding="utf-8")
        (git_bundle_dir() / "libexec" / "git-core").mkdir(parents=True, exist_ok=True)
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
            trust_repo(repo)
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


class SessionSlotTests(SandboxTestCase):
    def test_session_dir_name_encoding(self) -> None:
        self.assertEqual(session_dir_name("/workspace"), "--workspace--")
        self.assertEqual(session_dir_name("/home/a/proj"), "--home-a-proj--")

    def test_slot_mount_none_when_sessions_not_shared(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = SandboxConfig(share_host_sessions=False)
            self.assertIsNone(session_slot_mount(Path(tmp), cfg))

    def test_slot_mount_host_path_mode_uses_same_name_both_sides(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            slot = session_slot_mount(repo, SandboxConfig(guest_repo_mount="host_path"))
            assert slot is not None
            host_dir, guest_name = slot
            self.assertEqual(guest_name, session_dir_name(str(repo.resolve())))
            self.assertEqual(host_dir, self.sessions / guest_name)
            self.assertTrue(host_dir.is_dir())

    def test_slot_mount_workspace_mode_needs_no_symlink_on_host(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            slot = session_slot_mount(repo, SandboxConfig(guest_repo_mount="workspace"))
            assert slot is not None
            host_dir, guest_name = slot
            self.assertEqual(guest_name, "--workspace--")
            self.assertEqual(host_dir.name, session_dir_name(str(repo.resolve())))
            self.assertFalse((self.sessions / "--workspace--").exists())

    def test_stale_workspace_link_from_older_versions_is_removed(self) -> None:
        self.sessions.mkdir(parents=True)
        target = self.sessions / "--some-repo--"
        target.mkdir()
        link = self.sessions / "--workspace--"
        link.symlink_to(target)

        remove_stale_workspace_link()

        self.assertFalse(link.is_symlink())
        self.assertTrue(target.is_dir())

    def test_real_workspace_slot_dir_is_left_alone(self) -> None:
        real = self.sessions / "--workspace--"
        real.mkdir(parents=True)
        remove_stale_workspace_link()
        self.assertTrue(real.is_dir())

    def test_scrub_removes_symlinks_guest_left_in_slot(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            victim = Path(tmp) / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            cfg = SandboxConfig()
            slot = session_slot_mount(repo, cfg)
            assert slot is not None
            host_dir = slot[0]
            (host_dir / "real.jsonl").write_text("{}", encoding="utf-8")
            (host_dir / "planted.jsonl").symlink_to(victim)
            (host_dir / "nested").mkdir()
            (host_dir / "nested" / "dir-link").symlink_to(tmp)

            removed = scrub_session_symlinks(repo, cfg)

            self.assertEqual(len(removed), 2)
            self.assertTrue((host_dir / "real.jsonl").is_file())
            self.assertFalse((host_dir / "planted.jsonl").is_symlink())
            self.assertFalse((host_dir / "nested" / "dir-link").is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_scrub_is_noop_when_sessions_not_shared(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = SandboxConfig(share_host_sessions=False)
            self.assertEqual(scrub_session_symlinks(Path(tmp), cfg), [])


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


def _run_args(**overrides):
    from argparse import Namespace

    base = dict(
        model_endpoint=None,
        allow_hosts=[],
        providers=[],
        network_policy=None,
        vmm=None,
        image=None,
        rootfs_size=None,
        ro=False,
        guest_hidden_paths=[],
        git_commit=False,
        git_write=False,
        host_loopback=False,
        no_host_loopback=False,
        no_auto_approve=False,
        mirror_host_pi=False,
    )
    base.update(overrides)
    return Namespace(**base)


def _write_repo_config(repo: Path, data: dict) -> Path:
    path = repo / ".myai" / "sandbox.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TrustTests(SandboxTestCase):
    def test_repo_without_config_needs_no_trust(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertTrue(is_trusted(repo))
            self.assertEqual(load_trusted_config(repo).network_policy, "custom")

    def test_unapproved_repo_config_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"network_policy": "allow-all"})
            self.assertFalse(is_trusted(repo))
            with self.assertRaises(UntrustedConfigError) as ctx:
                load_trusted_config(repo)
            self.assertIn("myai sandbox trust", str(ctx.exception))

    def test_approved_config_loads_and_any_edit_revokes_it(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = _write_repo_config(repo, {"providers": ["anthropic"]})
            trust_repo(repo)
            self.assertEqual(load_trusted_config(repo).providers, ["anthropic"])

            path.write_text(json.dumps({"providers": ["anthropic"], "allow_hosts": ["evil.example"]}))
            self.assertFalse(is_trusted(repo))
            with self.assertRaises(UntrustedConfigError):
                load_trusted_config(repo)

    def test_whitespace_only_edit_also_needs_reapproval(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = _write_repo_config(repo, {"providers": ["anthropic"]})
            trust_repo(repo)
            path.write_text(path.read_text() + "\n")
            self.assertFalse(is_trusted(repo))

    def test_trust_is_per_repo_path(self) -> None:
        with TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            for repo in (a, b):
                _write_repo_config(repo, {"providers": ["anthropic"]})
            trust_repo(a)
            self.assertTrue(is_trusted(a))
            self.assertFalse(is_trusted(b))

    def test_revoke(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {})
            trust_repo(repo)
            self.assertTrue(revoke_repo(repo))
            self.assertFalse(is_trusted(repo))
            self.assertFalse(revoke_repo(repo))

    def test_ignore_repo_config_runs_on_global_alone(self) -> None:
        self.write_global({"providers": ["openai"]})
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"network_policy": "allow-all"})
            cfg = load_trusted_config(repo, ignore_repo_config=True)
            self.assertEqual(cfg.network_policy, "custom")
            self.assertEqual(cfg.providers, ["openai"])

    def test_corrupt_trust_store_trusts_nothing(self) -> None:
        from myai.paths import sandbox_trust_path

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {})
            trust_repo(repo)
            sandbox_trust_path().write_text("not json", encoding="utf-8")
            self.assertFalse(is_trusted(repo))

    def test_trust_store_is_private_and_outside_guest_mounted_dirs(self) -> None:
        from myai.paths import sandbox_root, sandbox_trust_path

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {})
            trust_repo(repo)
            store = sandbox_trust_path()
            self.assertEqual(store.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(sandbox_root(), store.parents)

    def test_cfg_from_args_refuses_untrusted_and_honors_ignore_flag(self) -> None:
        from myai.commands.sandbox import _cfg_from_args

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"network_policy": "allow-all"})
            with self.assertRaises(UntrustedConfigError):
                _cfg_from_args(repo, _run_args())
            cfg = _cfg_from_args(repo, _run_args(ignore_repo_config=True))
            self.assertEqual(cfg.network_policy, "custom")

    def test_init_trusts_what_it_writes_and_will_not_clobber(self) -> None:
        from argparse import Namespace

        from myai.commands.sandbox import run_init

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertEqual(run_init(Namespace(path=str(repo), force=False)), 0)
            self.assertTrue(is_trusted(repo))

            path = repo / ".myai" / "sandbox.json"
            path.write_text(json.dumps({"providers": ["anthropic"]}), encoding="utf-8")
            self.assertEqual(run_init(Namespace(path=str(repo), force=False)), 1)
            self.assertIn("anthropic", path.read_text(encoding="utf-8"))
            self.assertEqual(run_init(Namespace(path=str(repo), force=True)), 0)
            self.assertNotIn("anthropic", path.read_text(encoding="utf-8"))

    def test_trust_command_yes_approves_and_revoke_withdraws(self) -> None:
        from argparse import Namespace

        from myai.commands.sandbox import run_trust

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"providers": ["anthropic"]})
            self.assertEqual(run_trust(Namespace(path=str(repo), yes=True, revoke=False)), 0)
            self.assertTrue(is_trusted(repo))
            self.assertEqual(run_trust(Namespace(path=str(repo), yes=False, revoke=True)), 0)
            self.assertFalse(is_trusted(repo))

    def test_trust_command_declined_leaves_it_untrusted(self) -> None:
        from argparse import Namespace

        from myai.commands.sandbox import run_trust

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"providers": ["anthropic"]})
            with patch("builtins.input", return_value="n"):
                self.assertEqual(run_trust(Namespace(path=str(repo), yes=False, revoke=False)), 1)
            self.assertFalse(is_trusted(repo))

    def test_describe_grants_spells_out_what_matters(self) -> None:
        cfg = SandboxConfig(
            providers=["anthropic"],
            host_secrets=[HostSecret(name="X", hosts=["evil.example"], env_var="AWS_SECRET_ACCESS_KEY")],
            use_ssh_agent=True,
            ssh_allow_hosts=["github.com"],
            git_access="write",
        )
        cfg.host_loopback = HostLoopbackConfig(
            enabled=True,
            routes=[HostLoopbackRoute(id="db", guest_host="db.host", upstream="127.0.0.1:5432")],
        )
        text = "\n".join(describe_grants(cfg))
        self.assertIn("api.anthropic.com", text)
        self.assertIn("$AWS_SECRET_ACCESS_KEY sent to evil.example", text)
        self.assertIn("db.host:5432 -> 127.0.0.1:5432", text)
        self.assertIn("ssh agent is forwarded", text)
        self.assertIn("guest can WRITE the real .git", text)
        self.assertIn("auto-approves", text)

    def test_describe_grants_flags_unrestricted_egress(self) -> None:
        text = "\n".join(describe_grants(SandboxConfig(network_policy="allow-all")))
        self.assertIn("UNRESTRICTED", text)


class GlobalOnlyKeyTests(SandboxTestCase):
    def test_repo_cannot_choose_the_host_sdk_or_pi_package(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {
                "gondolin_package": "evil-pkg",
                "gondolin_version": "9.9.9",
                "pi_package": "evil-pi",
            })
            cfg = load_config(repo)
            self.assertEqual(gondolin_package_spec(cfg), "@earendil-works/gondolin@0.12.0")
            self.assertEqual(cfg.pi_package, "@earendil-works/pi-coding-agent")
            self.assertEqual(len(cfg.warnings), 3)

    def test_repo_url_version_is_dropped_before_it_can_fail_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"gondolin_version": "https://attacker.example/evil.tgz"})
            cfg = load_config(repo)
            self.assertEqual(cfg.gondolin_version, "0.12.0")
            self.assertIn("gondolin_version", cfg.warnings[0])

    def test_matching_value_in_old_full_dump_is_silent(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"gondolin_version": "0.12.0"})
            self.assertEqual(load_config(repo).warnings, [])

    def test_global_config_may_set_them(self) -> None:
        self.write_global({"gondolin_version": "0.13.1", "pi_package": "my-pi@1.0.0"})
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"gondolin_version": "0.12.0"})
            cfg = load_config(repo)
            self.assertEqual(cfg.gondolin_version, "0.13.1")
            self.assertEqual(cfg.pi_package, "my-pi@1.0.0")
            self.assertEqual(len(cfg.warnings), 1)

    def test_save_repo_config_leaves_them_out(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            save_repo_config(repo, default_sandbox_config())
            data = json.loads((repo / ".myai" / "sandbox.json").read_text(encoding="utf-8"))
            for key in ("gondolin_package", "gondolin_version", "pi_package"):
                self.assertNotIn(key, data)


class MergeTests(SandboxTestCase):
    def test_sparse_repo_file_does_not_reset_global_choices(self) -> None:
        self.write_global({"auto_approve": False, "mount_readonly": True, "network_policy": "deny-all"})
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"version": 2})
            cfg = load_config(repo)
            self.assertFalse(cfg.auto_approve)
            self.assertTrue(cfg.mount_readonly)
            self.assertEqual(cfg.network_policy, "deny-all")

    def test_repo_overrides_the_keys_it_names(self) -> None:
        self.write_global({"auto_approve": False, "providers": ["openai"]})
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_repo_config(repo, {"providers": ["anthropic"]})
            cfg = load_config(repo)
            self.assertEqual(cfg.providers, ["anthropic"])
            self.assertFalse(cfg.auto_approve)

    def test_non_object_config_is_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = repo / ".myai" / "sandbox.json"
            path.parent.mkdir()
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(SandboxConfigError):
                load_config(repo)


class ValidationTests(SandboxTestCase):
    def test_host_patterns(self) -> None:
        for ok in (
            "api.anthropic.com", "*.githubcopilot.com", "localhost", "127.0.0.1",
            "model.host:8080", DENY_ALL_SENTINEL,
        ):
            validate_host_pattern(ok, what="t")
        # gondolin's * matches any substring, dots included
        for bad in ("*", "*.*", "**", "*.com", "api.*", "*github.com", "a*b.example.com",
                    "has space.com", "", "http://x.com", None, 5):
            with self.assertRaises(SandboxConfigError, msg=repr(bad)):
                validate_host_pattern(bad, what="t")

    def test_wildcard_everything_in_allow_hosts_is_rejected(self) -> None:
        with self.assertRaises(SandboxConfigError) as ctx:
            SandboxConfig(allow_hosts=["*"]).validate()
        self.assertIn("allow-all", str(ctx.exception))

    def test_every_known_provider_domain_is_a_valid_pattern(self) -> None:
        from myai.sandbox.config import PROVIDER_DOMAINS

        for domains in PROVIDER_DOMAINS.values():
            for domain in domains:
                validate_host_pattern(domain, what="provider")

    def test_secret_hosts_and_names(self) -> None:
        HostSecret(name="ANTHROPIC_API_KEY", hosts=["api.anthropic.com"]).validate()
        HostSecret(name="X", hosts=["a.example.com"], env_var="REAL_NAME").validate()
        for bad in (
            HostSecret(name="X", hosts=["*"]),
            HostSecret(name="X", hosts=[]),
            HostSecret(name="has space", hosts=["a.example.com"]),
            HostSecret(name="X=1", hosts=["a.example.com"]),
            HostSecret(name="X", hosts=["a.example.com"], env_var="A B"),
            HostSecret(name="NODE_OPTIONS", hosts=["a.example.com"]),
            HostSecret(name="path", hosts=["a.example.com"]),
            HostSecret(name="LD_PRELOAD", hosts=["a.example.com"]),
        ):
            with self.assertRaises(SandboxConfigError, msg=repr(bad)):
                bad.validate()

    def test_gondolin_version_must_be_exact(self) -> None:
        SandboxConfig(gondolin_version="0.12.0").validate()
        SandboxConfig(gondolin_version="1.0.0-beta.2").validate()
        SandboxConfig(gondolin_version="latest").validate()
        for bad in ("https://attacker.example/evil.tgz", "github:a/b", "^0.12.0", "0.12",
                    "file:../x", "0.12.0 || 1", ""):
            with self.assertRaises(SandboxConfigError, msg=bad):
                SandboxConfig(gondolin_version=bad).validate()

    def test_package_names(self) -> None:
        SandboxConfig(gondolin_package="@scope/pkg", pi_package="@scope/pi@1.2.3").validate()
        SandboxConfig(pi_package="pi-agent@next").validate()
        for bad in ("evil; rm -rf /", "../x", "https://x/y.tgz", "a b", "$(id)", ""):
            with self.assertRaises(SandboxConfigError, msg=bad):
                SandboxConfig(gondolin_package=bad).validate()
        for bad in ("left-pad; touch /opt/pi/INJECTED #", "git+https://x/y", "a@b@c", "$(id)", "`id`"):
            with self.assertRaises(SandboxConfigError, msg=bad):
                SandboxConfig(pi_package=bad).validate()

    def test_provision_shell_quotes_the_package(self) -> None:
        cfg = SandboxConfig(pi_package="@scope/pi@1.2.3")
        script = build_provision_shell(cfg)[1][1]
        self.assertIn("--ignore-scripts '@scope/pi@1.2.3';", script)


class HiddenPathTests(SandboxTestCase):
    def test_myai_is_hidden_even_if_config_drops_it(self) -> None:
        for paths in ([], ["/secrets"], ["/.myai"]):
            cfg = SandboxConfig(guest_hidden_paths=paths)
            self.assertEqual(effective_hidden_paths(cfg)[0], "/.myai")
            self.assertEqual(effective_hidden_paths(cfg).count("/.myai"), 1)

    def test_run_spec_always_hides_myai(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = SandboxConfig(guest_hidden_paths=["/secrets"], install_pi_at_boot=False)
            plan = build_run_plan(Path(tmp), cfg, [])
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            plan.spec_path.unlink()
            self.assertEqual(data["vfs"]["workspace"]["hiddenPaths"], ["/.myai", "/secrets"])

    def test_hide_flag_adds_to_the_list(self) -> None:
        from myai.commands.sandbox import _cfg_from_args

        with TemporaryDirectory() as tmp:
            cfg = _cfg_from_args(Path(tmp), _run_args(guest_hidden_paths=["/secrets", "/.myai"]))
            self.assertEqual(cfg.guest_hidden_paths, ["/.myai", "/secrets"])


class GitAccessTests(SandboxTestCase):
    def _workspace(self, cfg: SandboxConfig) -> dict:
        with TemporaryDirectory() as tmp:
            plan = build_run_plan(Path(tmp), cfg, [])
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            plan.spec_path.unlink()
            return data["vfs"]["workspace"]

    def test_read_only_is_the_default(self) -> None:
        self.assertEqual(SandboxConfig().git_access, "read-only")
        self.assertTrue(self._workspace(SandboxConfig(install_pi_at_boot=False))["gitReadonly"])

    def test_commit_mode_keeps_real_git_read_only(self) -> None:
        # commit mode must NOT make the real .git writable; it uses a scratch clone
        self.assertTrue(self._workspace(SandboxConfig(git_access="commit", install_pi_at_boot=False))["gitReadonly"])

    def test_write_mode_lifts_it(self) -> None:
        self.assertFalse(self._workspace(SandboxConfig(git_access="write", install_pi_at_boot=False))["gitReadonly"])

    def test_flags_map_to_access(self) -> None:
        from myai.commands.sandbox import _cfg_from_args

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertEqual(_cfg_from_args(repo, _run_args(git_commit=True)).git_access, "commit")
            self.assertEqual(_cfg_from_args(repo, _run_args(git_write=True)).git_access, "write")
            self.assertEqual(_cfg_from_args(repo, _run_args()).git_access, "read-only")

    def test_roundtrip_and_legacy_key(self) -> None:
        self.assertEqual(_config_from_dict(_config_to_dict(SandboxConfig(git_access="commit"))).git_access, "commit")
        self.assertEqual(_config_from_dict({"guest_git_readonly": False}).git_access, "write")
        self.assertEqual(_config_from_dict({"guest_git_readonly": True}).git_access, "read-only")

    def test_invalid_access_rejected(self) -> None:
        with self.assertRaises(SandboxConfigError):
            SandboxConfig(git_access="sometimes").validate()


class CacheMountTests(SandboxTestCase):
    def _mounts(self, plan) -> dict:
        data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
        plan.spec_path.unlink()
        return {m["guestPath"]: m for m in data["vfs"]["mounts"]}

    def test_pi_caches_are_read_only_at_runtime(self) -> None:
        with TemporaryDirectory() as tmp:
            mounts = self._mounts(build_run_plan(Path(tmp), SandboxConfig(mirror_host_pi=True), []))
            for guest in ("/opt/pi", f"{GUEST_AGENT_PATH}/bin", f"{GUEST_AGENT_PATH}/npm", f"{GUEST_AGENT_PATH}/git"):
                self.assertTrue(mounts[guest]["readonly"], guest)
            # pi keeps per-run state here; wiped before every run
            self.assertFalse(mounts[GUEST_AGENT_PATH].get("readonly", False))

    def test_only_the_install_vm_can_write_them(self) -> None:
        with TemporaryDirectory() as tmp:
            mounts = self._mounts(build_provision_plan(Path(tmp), SandboxConfig(mirror_host_pi=True)))
            for guest in ("/opt/pi", f"{GUEST_AGENT_PATH}/bin", f"{GUEST_AGENT_PATH}/npm"):
                self.assertFalse(mounts[guest]["readonly"], guest)


class LoopbackPolicyTests(SandboxTestCase):
    def test_deny_all_means_no_host_ports_either(self) -> None:
        cfg = _loopback_cfg(network_policy="deny-all")
        cfg.validate()
        self.assertFalse(resolve_host_loopback_enabled(cfg))
        self.assertEqual(resolve_host_loopback_routes(cfg), [])
        self.assertEqual(render_models_json(cfg), "{}\n")
        with TemporaryDirectory() as tmp:
            plan = build_run_plan(Path(tmp), cfg, [])
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            plan.spec_path.unlink()
            self.assertEqual(data["network"]["tcpHosts"], {})
            self.assertEqual(data["network"]["allowedHosts"], [DENY_ALL_SENTINEL])

    def test_deny_all_beats_the_env_switch(self) -> None:
        os.environ["MYAI_HOST_LOOPBACK"] = "1"
        self.assertFalse(resolve_host_loopback_enabled(SandboxConfig(network_policy="deny-all")))

    def test_custom_with_empty_allow_list_still_reaches_loopback(self) -> None:
        hosts, unrestricted = runtime_allow_host_args(_loopback_cfg())
        self.assertFalse(unrestricted)
        self.assertIn("model.host", hosts)

    def _route_cfg(self, upstream: str, *, allow_remote: bool = False) -> SandboxConfig:
        cfg = SandboxConfig()
        cfg.host_loopback = HostLoopbackConfig(
            enabled=True,
            allow_remote_upstreams=allow_remote,
            routes=[HostLoopbackRoute(id="r", guest_host="r.host", upstream=upstream)],
        )
        return cfg

    def test_loopback_upstreams_are_fine(self) -> None:
        for upstream in ("http://localhost:8080/v1", "127.0.0.1:5432", "http://[::1]:9000", "127.1.2.3:80"):
            self._route_cfg(upstream).validate()

    def test_other_machines_need_the_opt_in(self) -> None:
        for upstream in ("http://192.168.1.50:8080/v1", "10.0.0.5:5432", "http://nas.local:8080", "8.8.8.8:53"):
            with self.assertRaises(SandboxConfigError, msg=upstream) as ctx:
                self._route_cfg(upstream).validate()
            self.assertIn("allow_remote_upstreams", str(ctx.exception))
            self._route_cfg(upstream, allow_remote=True).validate()

    def test_link_local_is_never_bridged(self) -> None:
        for upstream in ("169.254.169.254:80", "http://169.254.169.254/latest", "0.0.0.0:80", "http://[fe80::1]:80"):
            with self.assertRaises(SandboxConfigError, msg=upstream):
                self._route_cfg(upstream, allow_remote=True).validate()

    def test_bad_route_is_caught_at_use_even_if_only_env_enables_loopback(self) -> None:
        cfg = self._route_cfg("10.0.0.5:5432")
        cfg.host_loopback.enabled = False
        cfg.validate()
        os.environ["MYAI_HOST_LOOPBACK"] = "1"
        with self.assertRaises(SandboxConfigError):
            resolve_host_loopback_routes(cfg)

    def test_allow_remote_upstream_flag_and_roundtrip(self) -> None:
        from myai.commands.sandbox import _cfg_from_args

        with TemporaryDirectory() as tmp:
            args = _run_args(model_endpoint="http://192.168.1.50:8080/v1")
            with self.assertRaises(SandboxConfigError):
                _cfg_from_args(Path(tmp), args)
            args.allow_remote_upstream = True
            cfg = _cfg_from_args(Path(tmp), args)
            self.assertTrue(cfg.host_loopback.allow_remote_upstreams)
            again = _config_from_dict(_config_to_dict(cfg))
            self.assertTrue(again.host_loopback.allow_remote_upstreams)


class CheckMountableTests(SandboxTestCase):
    def test_project_dir_is_fine(self) -> None:
        with TemporaryDirectory() as tmp:
            check_mountable(Path(tmp))

    def test_refuses_dirs_that_contain_myai_state_or_home(self) -> None:
        state = Path(self._tmp.name)
        with self.assertRaises(GondolinError):
            check_mountable(state)
        with self.assertRaises(GondolinError):
            check_mountable(state.parent)
        with self.assertRaises(GondolinError):
            check_mountable(Path.home())
        with self.assertRaises(GondolinError):
            check_mountable(Path("/"))


def _parse(lines):
    from myai.sandbox.vm_spec import _parse_env_lines
    return _parse_env_lines(lines)


class GitBundleTests(SandboxTestCase):
    def test_provision_shell_stages_git_into_persistent_mounts(self) -> None:
        script = build_git_bundle_shell()
        self.assertIn("apk add --no-cache git", script)
        self.assertIn(f"{GUEST_AGENT_PATH}/bin/git", script)   # binary -> pi-bin (on PATH)
        self.assertIn("/opt/git/libexec/git-core", script)     # helpers -> bundle
        self.assertIn("/opt/git/lib", script)                  # shared libs -> bundle
        self.assertIn("cp -a", script)                         # keep git-core hardlinks
        # skip the work if it is already staged
        self.assertIn(f'if ! [ -x "{GUEST_AGENT_PATH}/bin/git"', script)

    def test_build_provision_shell_includes_git_bundle(self) -> None:
        _, args = build_provision_shell(SandboxConfig(install_pi_at_boot=True))
        self.assertIn("/opt/git/libexec/git-core", args[1])

    def test_run_spec_mounts_git_bundle_read_only(self) -> None:
        with TemporaryDirectory() as tmp:
            plan = build_run_plan(Path(tmp), SandboxConfig(), [])
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            plan.spec_path.unlink()
            mounts = {m["guestPath"]: m for m in data["vfs"]["mounts"]}
            self.assertIn("/opt/git", mounts)
            self.assertTrue(mounts["/opt/git"]["readonly"])

    def test_provision_spec_mounts_git_bundle_writable(self) -> None:
        with TemporaryDirectory() as tmp:
            plan = build_provision_plan(Path(tmp), SandboxConfig())
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            plan.spec_path.unlink()
            mounts = {m["guestPath"]: m for m in data["vfs"]["mounts"]}
            self.assertFalse(mounts["/opt/git"]["readonly"])

    def test_guest_env_wires_git_onto_path_and_exec(self) -> None:
        env = _parse(guest_agent_env(SandboxConfig()))
        self.assertEqual(env["GIT_EXEC_PATH"], "/opt/git/libexec/git-core")
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/git/lib")
        self.assertIn(f"{GUEST_AGENT_PATH}/bin", env["PATH"].split(":"))

    def test_alpine_mirror_allowed_at_provision_for_git(self) -> None:
        # git is always apk-installed during provisioning, so the mirror is allowed
        self.assertIn(ALPINE_MIRROR_HOST, provision_allow_hosts(SandboxConfig()))
        self.assertNotIn(ALPINE_MIRROR_HOST, effective_allow_hosts(SandboxConfig()))

    def test_commit_mode_wires_git_dir_and_worktree(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            env = _parse(guest_agent_env(SandboxConfig(git_access="commit"), repo=repo))
            self.assertEqual(env["GIT_DIR"], "/root/agent-git")
            self.assertEqual(env["GIT_WORK_TREE"], str(repo.resolve()))
            self.assertTrue(env["GIT_CONFIG_GLOBAL"].endswith("/gitconfig"))

    def test_no_git_dir_when_not_commit_mode_or_not_a_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertNotIn("GIT_DIR", _parse(guest_agent_env(SandboxConfig(), repo=repo)))
            (repo / ".git").mkdir()
            self.assertIn("GIT_DIR", _parse(guest_agent_env(SandboxConfig(git_access="commit"), repo=repo)))

    def test_commit_mode_mounts_scratch_git_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            plan = build_run_plan(repo, SandboxConfig(git_access="commit"), [])
            data = json.loads(plan.spec_path.read_text(encoding="utf-8"))
            plan.spec_path.unlink()
            guest_paths = [m["guestPath"] for m in data["vfs"]["mounts"]]
            self.assertIn("/root/agent-git", guest_paths)

    def test_gitconfig_written_for_default_image(self) -> None:
        with TemporaryDirectory() as tmp:
            staging = prepare_agent_dir(Path(tmp), SandboxConfig())
            text = (staging / "gitconfig").read_text(encoding="utf-8")
            self.assertIn("directory = *", text)
            self.assertIn("[user]", text)


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
