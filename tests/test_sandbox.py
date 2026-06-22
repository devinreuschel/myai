import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.sandbox.config import (
    DEFAULT_MODEL_ENDPOINT,
    GUEST_AGENT_PATH,
    HostLoopbackConfig,
    HostLoopbackRoute,
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
    gondolin_invocation,
    host_sessions_dir,
    load_config,
    provision_allow_hosts,
    resolve_host_loopback_enabled,
    resolve_host_loopback_routes,
    resolve_model_endpoint,
    rewrite_endpoint_for_guest,
    save_repo_config,
)
from myai.sandbox.doctor import doctor_ok, run_doctor
from myai.sandbox.gondolin import build_provision_plan, build_run_plan
from myai.sandbox.provision import (
    build_pi_launch_shell,
    build_provision_shell,
    guest_agent_env,
    is_provisioned,
    needs_provision,
    pi_bin_dir,
    pi_install_dir,
    prepare_agent_dir,
    render_guest_settings,
    render_models_json,
)


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
            cfg = SandboxConfig(model_endpoint="http://localhost:8080/v1", warm_reuse=False)
            save_repo_config(repo, cfg)
            loaded = load_config(repo)
            self.assertFalse(loaded.warm_reuse)
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

    def test_gondolin_invocation_pin(self) -> None:
        cfg = SandboxConfig(gondolin_version="1.2.3")
        self.assertEqual(
            gondolin_invocation(cfg),
            ["npx", "--yes", "@earendil-works/gondolin@1.2.3"],
        )

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


class GondolinPlanTests(SandboxTestCase):
    def test_build_run_plan_no_tcp_map_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False)
            plan = build_run_plan(repo, cfg, ["-p", "hello"])
            self.assertIn("bash", plan.cmd)
            mount_idx = plan.cmd.index("--mount-hostfs")
            self.assertTrue(plan.cmd[mount_idx + 1].endswith(f":{repo.resolve()}"))
            cwd_idx = plan.cmd.index("--cwd")
            self.assertEqual(plan.cmd[cwd_idx + 1], str(repo.resolve()))
            self.assertNotIn("--tcp-map", plan.cmd)
            dash = plan.cmd.index("--")
            self.assertEqual(plan.cmd[dash + 1], "pi")
            self.assertIn("-a", plan.cmd[dash + 2 :])
            self.assertNotIn("--provider", plan.cmd[dash + 2 :])

    def test_build_run_plan_legacy_loopback(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = _loopback_cfg(install_pi_at_boot=False)
            plan = build_run_plan(repo, cfg, ["-p", "hello"])
            self.assertIn("--tcp-map", plan.cmd)
            self.assertIn("model.host:8080=127.0.0.1:8080", plan.cmd)
            self.assertIn("--env", plan.cmd)
            self.assertIn("PI_CODING_AGENT_DIR=/root/.pi/agent", plan.cmd)
            dash = plan.cmd.index("--")
            self.assertIn("--provider", plan.cmd[dash + 2 :])

    def test_build_run_plan_multi_route(self) -> None:
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
            plan = build_run_plan(repo, cfg, [])
            tcp_maps = [
                plan.cmd[i + 1]
                for i, flag in enumerate(plan.cmd)
                if flag == "--tcp-map"
            ]
            self.assertEqual(
                tcp_maps,
                ["model.host:8080=127.0.0.1:8080", "mcp.host:6277=127.0.0.1:6277"],
            )

    def test_host_secret_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            from myai.sandbox.config import HostSecret

            cfg = SandboxConfig(
                install_pi_at_boot=False,
                host_secrets=[HostSecret(name="OPENAI_API_KEY", hosts=["api.openai.com"])],
            )
            plan = build_run_plan(repo, cfg, [])
            self.assertIn("--host-secret", plan.cmd)
            self.assertIn("OPENAI_API_KEY@api.openai.com", plan.cmd)

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

    def test_build_run_plan_rootfs_size_when_explicit(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(rootfs_size="4G", install_pi_at_boot=True, image="alpine-base:latest")
            plan = build_run_plan(repo, cfg, [])
            idx = plan.cmd.index("--rootfs-size")
            self.assertEqual(plan.cmd[idx + 1], "4G")

    def test_build_run_plan_pi_install_mount(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
            plan = build_run_plan(repo, cfg, [])
            mounts = [
                plan.cmd[i + 1]
                for i, flag in enumerate(plan.cmd)
                if flag == "--mount-hostfs"
            ]
            self.assertTrue(any(m.endswith(":/opt/pi") for m in mounts))
            self.assertTrue(any(m.endswith(f":{GUEST_AGENT_PATH}/bin") for m in mounts))
            self.assertNotIn("--rootfs-size", plan.cmd)

    def test_build_run_plan_runtime_no_github_allow(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
            plan = build_run_plan(repo, cfg, [])
            allow_hosts = [
                plan.cmd[i + 1]
                for i, flag in enumerate(plan.cmd)
                if flag == "--allow-host"
            ]
            self.assertNotIn("github.com", allow_hosts)
            self.assertNotIn("registry.npmjs.org", allow_hosts)

    def test_build_provision_plan_allows_github(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest")
            plan = build_provision_plan(repo, cfg)
            allow_hosts = [
                plan.cmd[i + 1]
                for i, flag in enumerate(plan.cmd)
                if flag == "--allow-host"
            ]
            self.assertIn("github.com", allow_hosts)
            self.assertIn("registry.npmjs.org", allow_hosts)
            self.assertNotIn("--tcp-map", plan.cmd)

    def test_build_run_plan_mirror_pkg_mounts(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=True, image="alpine-base:latest", mirror_host_pi=True)
            plan = build_run_plan(repo, cfg, [])
            mounts = [
                plan.cmd[i + 1]
                for i, flag in enumerate(plan.cmd)
                if flag == "--mount-hostfs"
            ]
            self.assertTrue(any(m.endswith(f":{GUEST_AGENT_PATH}/npm") for m in mounts))
            self.assertTrue(any(m.endswith(f":{GUEST_AGENT_PATH}/git") for m in mounts))

    def test_build_run_plan_no_rootfs_size_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(install_pi_at_boot=False, image="alpine-base:latest")
            plan = build_run_plan(repo, cfg, [])
            self.assertNotIn("--rootfs-size", plan.cmd)

    def test_build_run_plan_workspace_mount_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(guest_repo_mount="workspace", install_pi_at_boot=False)
            plan = build_run_plan(repo, cfg, [])
            mount = plan.cmd[plan.cmd.index("--mount-hostfs") + 1]
            self.assertTrue(mount.endswith(":/workspace"))
            cwd_idx = plan.cmd.index("--cwd")
            self.assertEqual(plan.cmd[cwd_idx + 1], WORKSPACE_PATH)

    def test_build_run_plan_shares_host_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(share_host_sessions=True, install_pi_at_boot=False)
            plan = build_run_plan(repo, cfg, [])
            mounts = [
                plan.cmd[i + 1]
                for i, flag in enumerate(plan.cmd)
                if flag == "--mount-hostfs"
            ]
            expected = f"{host_sessions_dir().resolve()}:{GUEST_AGENT_PATH}/sessions"
            self.assertIn(expected, mounts)

    def test_build_run_plan_no_sessions_mount_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = SandboxConfig(share_host_sessions=False, install_pi_at_boot=False)
            plan = build_run_plan(repo, cfg, [])
            mounts = [
                plan.cmd[i + 1]
                for i, flag in enumerate(plan.cmd)
                if flag == "--mount-hostfs"
            ]
            self.assertFalse(any(m.endswith(f":{GUEST_AGENT_PATH}/sessions") for m in mounts))


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
                vmm=None,
                image=None,
                no_warm=False,
                ro=False,
                host_loopback=False,
                no_host_loopback=False,
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
                vmm=None,
                image=None,
                no_warm=False,
                ro=False,
                host_loopback=False,
                no_host_loopback=True,
            )
            cfg = _cfg_from_args(repo, args)
            self.assertFalse(cfg.host_loopback.enabled)


class DoctorTests(unittest.TestCase):
    def test_run_doctor_returns_checks(self) -> None:
        results = run_doctor()
        names = {r.name for r in results}
        self.assertIn("node", names)
        self.assertIn("qemu", names)
        self.assertIn("disk", names)

    def test_doctor_ok_requires_core(self) -> None:
        from myai.sandbox.doctor import CheckResult

        ok = [
            CheckResult("node", True, ""),
            CheckResult("npx", True, ""),
            CheckResult("qemu", True, ""),
            CheckResult("virtualization", True, ""),
            CheckResult("disk", True, ""),
            CheckResult("krun", False, "", "optional"),
        ]
        self.assertTrue(doctor_ok(ok))


if __name__ == "__main__":
    unittest.main()
