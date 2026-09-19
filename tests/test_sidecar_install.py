import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.sandbox.config import (
    SandboxConfig,
    SandboxConfigError,
    sidecar_install_dir,
    sidecar_source_dir,
)
from myai.sandbox.sidecar_install import (
    SidecarError,
    ensure_sidecar_installed,
    is_sidecar_installed,
)


class _StateDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home
        self._tmp.cleanup()


@unittest.skipUnless(shutil.which("node"), "node not installed")
@unittest.skipUnless(shutil.which("npm"), "npm not installed")
class SidecarInstallTests(_StateDirTestCase):
    def test_ensure_sidecar_installs_gondolin(self) -> None:
        cfg = SandboxConfig(gondolin_version="0.12.0")
        ensure_sidecar_installed(cfg, quiet=True)
        self.assertTrue(is_sidecar_installed(cfg))
        install = sidecar_install_dir()
        self.assertTrue((install / "guard.mjs").is_file())
        # installed from the shipped lockfile, byte for byte
        self.assertEqual(
            (install / "package-lock.json").read_bytes(),
            (sidecar_source_dir() / "package-lock.json").read_bytes(),
        )


class SidecarNpmInvocationTests(_StateDirTestCase):
    """The one npm run that happens on the host, not in a VM."""

    def _install(self, cfg: SandboxConfig) -> list[str]:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            sdk = sidecar_install_dir() / "node_modules" / "@earendil-works" / "gondolin"
            sdk.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("myai.sandbox.sidecar_install.subprocess.run", side_effect=fake_run):
            ensure_sidecar_installed(cfg, quiet=True)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_default_sdk_uses_npm_ci_with_the_shipped_lockfile(self) -> None:
        cmd = self._install(SandboxConfig())
        self.assertEqual(cmd[:2], ["npm", "ci"])
        self.assertIn("--ignore-scripts", cmd)
        self.assertTrue((sidecar_install_dir() / "package-lock.json").is_file())

    def test_custom_sdk_falls_back_to_install_without_a_stale_lockfile(self) -> None:
        self._install(SandboxConfig())
        cmd = self._install(SandboxConfig(gondolin_version="0.13.1"))
        self.assertEqual(cmd[:2], ["npm", "install"])
        self.assertIn("--ignore-scripts", cmd)
        install = sidecar_install_dir()
        self.assertFalse((install / "package-lock.json").exists())
        pkg = json.loads((install / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["dependencies"], {"@earendil-works/gondolin": "0.13.1"})

    def test_invalid_package_never_reaches_npm(self) -> None:
        for cfg in (
            SandboxConfig(gondolin_version="https://attacker.example/evil.tgz"),
            SandboxConfig(gondolin_package="evil; curl x | sh"),
        ):
            with patch("myai.sandbox.sidecar_install.subprocess.run") as run:
                with self.assertRaises(SandboxConfigError):
                    ensure_sidecar_installed(cfg, quiet=True)
                run.assert_not_called()

    def test_failed_install_leaves_no_installed_marker(self) -> None:
        self._install(SandboxConfig())
        self.assertTrue(is_sidecar_installed(SandboxConfig()))

        failed = subprocess.CompletedProcess([], 1, "", "boom")
        other = SandboxConfig(gondolin_version="0.13.1")
        with patch("myai.sandbox.sidecar_install.subprocess.run", return_value=failed):
            with self.assertRaises(SidecarError):
                ensure_sidecar_installed(other, quiet=True)
        self.assertFalse(is_sidecar_installed(other))
        self.assertFalse(is_sidecar_installed(SandboxConfig()))

    def test_missing_guard_counts_as_not_installed(self) -> None:
        self._install(SandboxConfig())
        (sidecar_install_dir() / "guard.mjs").unlink()
        self.assertFalse(is_sidecar_installed(SandboxConfig()))

    def test_shipped_lockfile_matches_package_json_and_pins_by_hash(self) -> None:
        source = sidecar_source_dir()
        pkg = json.loads((source / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((source / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["packages"][""]["dependencies"], pkg["dependencies"])
        entries = {k: v for k, v in lock["packages"].items() if k}
        self.assertTrue(entries)
        for name, entry in entries.items():
            self.assertTrue(entry.get("integrity", "").startswith("sha512-"), name)
            self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"), name)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class GuardTests(unittest.TestCase):
    def test_workspace_guard_node_tests_pass(self) -> None:
        test_file = Path(sidecar_source_dir()) / "guard.test.mjs"
        result = subprocess.run(
            ["node", "--test", str(test_file)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
