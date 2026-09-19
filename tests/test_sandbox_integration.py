import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.sandbox.config import SandboxConfig, sidecar_invocation
from myai.sandbox.sidecar_install import ensure_sidecar_installed


@unittest.skipUnless(shutil.which("node"), "node not installed")
@unittest.skipUnless(shutil.which("npm"), "npm not installed")
@unittest.skipUnless(os.environ.get("MYAI_SANDBOX_INTEGRATION"), "set MYAI_SANDBOX_INTEGRATION=1 to run")
class SandboxSidecarIntegrationTests(unittest.TestCase):
    """Opt-in VM integration: verifies sidecar boots and .myai is hidden."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = self._tmp.name
        # building a spec creates the repo's session slot; keep that out of the
        # real ~/.pi/agent/sessions
        self._sessions_patch = patch(
            "myai.sandbox.provision.host_sessions_dir",
            return_value=Path(self._tmp.name) / "sessions",
        )
        self._sessions_patch.start()

    def tearDown(self) -> None:
        self._sessions_patch.stop()
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_sidecar_spec_loads(self) -> None:
        """Smoke test: sidecar script loads and parses a minimal spec."""
        repo = Path(self._tmp.name) / "proj"
        repo.mkdir()
        (repo / ".myai").mkdir()
        (repo / ".myai" / "sandbox.json").write_text("{}", encoding="utf-8")

        cfg = SandboxConfig(install_pi_at_boot=False, network_policy="allow-all")
        ensure_sidecar_installed(cfg, quiet=True)

        from myai.sandbox.vm_spec import build_run_spec

        plan = build_run_spec(repo, cfg, ["--help"])
        spec_path = Path(self._tmp.name) / "spec.json"
        spec_path.write_text(json.dumps(plan.spec), encoding="utf-8")
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        self.assertIn("/.myai", data["vfs"]["workspace"]["hiddenPaths"])

    def test_guest_cannot_write_git_or_reach_myai(self) -> None:
        """Boots a VM and attacks the workspace guard from inside the guest."""
        tmp = Path(self._tmp.name).resolve()
        repo = tmp / "proj"
        (repo / ".myai").mkdir(parents=True)
        (repo / ".myai" / "sandbox.json").write_text("{}", encoding="utf-8")
        (repo / ".git" / "hooks").mkdir(parents=True)
        (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (repo / "src").mkdir()

        checks = {
            "git_read": "cat .git/config >/dev/null",
            "git_hook": "echo x > .git/hooks/pre-commit",
            "git_config": "echo x >> .git/config",
            "git_alias": "ln -s .git g && echo x > g/hooks/post-merge",
            "git_move": "mv .git .git2",
            "git_nested": "mkdir src/.git",
            "myai_read": "cat .myai/sandbox.json",
            "myai_case": "cat .MYAI/sandbox.json",
            "myai_alias": "ln -s .myai m && echo x > m/planted.json",
            "work": "echo hi > src/new.txt",
            "pi_cache": "touch /opt/pi/x",
        }
        script = 'cd "$WS" || exit 9\n' + "\n".join(
            f'( {cmd} ) >/dev/null 2>&1 && echo "{name}=yes" || echo "{name}=no"'
            for name, cmd in checks.items()
        )

        cfg = SandboxConfig()
        ensure_sidecar_installed(cfg, quiet=True)
        from myai.sandbox.vm_spec import build_run_spec

        spec = build_run_spec(repo, cfg, []).spec
        spec.update(
            command=["sh", "-lc", script],
            interactive=False,
            env={**spec["env"], "WS": spec["cwd"]},
        )
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

        result = subprocess.run(
            [*sidecar_invocation(cfg), str(spec_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        got = dict(line.split("=", 1) for line in result.stdout.split() if "=" in line)

        allowed = {"git_read", "work"}
        for name in checks:
            self.assertEqual(got.get(name), "yes" if name in allowed else "no", f"{name}: {result.stderr}")
        self.assertEqual(list((repo / ".git" / "hooks").iterdir()), [])
        self.assertEqual((repo / ".git" / "config").read_text(encoding="utf-8"), "[core]\n")
        self.assertEqual([p.name for p in (repo / ".myai").iterdir()], ["sandbox.json"])

    def test_commit_mode_end_to_end(self) -> None:
        """Provision git into a persistent bundle, then commit from a networkless
        run VM and import the result on the host."""
        import subprocess

        from myai.sandbox.agent_git import import_refs, seed
        from myai.sandbox.config import GIT_BUNDLE_MOUNT, sidecar_invocation
        from myai.sandbox.provision import (
            build_git_bundle_shell,
            git_bundle_dir,
            git_bundle_ready,
            pi_bin_dir,
        )
        from myai.sandbox.vm_spec import build_run_spec

        tmp = Path(self._tmp.name).resolve()
        repo = tmp / "proj"
        repo.mkdir()

        def git(*args, **env):
            e = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp / "gc"),
                 "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "h",
                 "GIT_AUTHOR_EMAIL": "h@x", "GIT_COMMITTER_NAME": "h",
                 "GIT_COMMITTER_EMAIL": "h@x", **env}
            subprocess.run(["git", *args], cwd=repo, env=e, check=True,
                           capture_output=True)

        git("init", "-q", "-b", "main")
        (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
        git("add", "-A"); git("commit", "-qm", "host: initial")
        before_main = subprocess.run(["git", "rev-parse", "main"], cwd=repo,
                                     capture_output=True, text=True).stdout

        cfg = SandboxConfig(install_pi_at_boot=False)
        ensure_sidecar_installed(cfg, quiet=True)

        # provision just the git bundle (network to the alpine mirror)
        pi_bin_dir().mkdir(parents=True, exist_ok=True)
        git_bundle_dir().mkdir(parents=True, exist_ok=True)
        prov = {
            "mode": "provision", "image": cfg.image, "vmm": "qemu",
            "network": {"policy": "custom", "allowedHosts": ["dl-cdn.alpinelinux.org"],
                        "secrets": {}, "tcpHosts": {}, "sshAllowHosts": [], "useSshAgent": False},
            "vfs": {"workspace": {"hostPath": str(tmp), "guestPath": "/w", "readonly": False,
                                  "hiddenPaths": [], "gitReadonly": False},
                    "mounts": [{"hostPath": str(pi_bin_dir().resolve()),
                                "guestPath": "/root/.pi/agent/bin"},
                               {"hostPath": str(git_bundle_dir().resolve()),
                                "guestPath": GIT_BUNDLE_MOUNT}], "memfs": ["/tmp"]},
            "cwd": "/w", "env": {"TERM": "xterm-256color"},
            "command": ["sh", "-lc", "set -e; " + build_git_bundle_shell() + "echo done"],
            "interactive": False,
        }
        (tmp / "prov.json").write_text(json.dumps(prov), encoding="utf-8")
        subprocess.run([*sidecar_invocation(cfg), str(tmp / "prov.json")],
                       check=False, timeout=600)
        self.assertTrue(git_bundle_ready())

        # run in commit mode, no network, only the persisted git
        run_cfg = SandboxConfig(install_pi_at_boot=True, git_access="commit")
        seed(repo, run_cfg)
        spec = build_run_spec(repo, run_cfg, []).spec
        script = (
            'cd "$WS"; git checkout -q -b agent/work && '
            'printf "print(2)\\n" >> a.py && git commit -qam "agent: work"; '
            '(echo x > "$WS/.git/hooks/pre-commit") 2>/dev/null && echo REALGIT_BAD || true'
        )
        spec.update(command=["sh", "-c", script], interactive=False,
                    env={**spec["env"], "WS": spec["cwd"]})
        (tmp / "run.json").write_text(json.dumps(spec), encoding="utf-8")
        result = subprocess.run([*sidecar_invocation(run_cfg), str(tmp / "run.json")],
                                capture_output=True, text=True, check=False, timeout=600)
        self.assertNotIn("REALGIT_BAD", result.stdout)

        imported = import_refs(repo, run_cfg, run_id="itest")
        refs = {ref for ref, _ in imported}
        self.assertIn("refs/sandbox/itest/agent/work", refs)
        # host repo untouched, and the agent's commit is a real, readable commit
        after_main = subprocess.run(["git", "rev-parse", "main"], cwd=repo,
                                    capture_output=True, text=True).stdout
        self.assertEqual(before_main, after_main)
        self.assertFalse((repo / ".git" / "hooks" / "pre-commit").exists())
        show = subprocess.run(
            ["git", "show", "refs/sandbox/itest/agent/work:a.py"], cwd=repo,
            capture_output=True, text=True, check=True)
        self.assertIn("print(2)", show.stdout)

