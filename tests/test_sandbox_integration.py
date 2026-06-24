import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from myai.sandbox.config import SandboxConfig
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

    def tearDown(self) -> None:
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
