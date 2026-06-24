import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myai.sandbox.config import SandboxConfig
from myai.sandbox.sidecar_install import ensure_sidecar_installed, is_sidecar_installed


@unittest.skipUnless(shutil.which("node"), "node not installed")
@unittest.skipUnless(shutil.which("npm"), "npm not installed")
class SidecarInstallTests(unittest.TestCase):
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

    def test_ensure_sidecar_installs_gondolin(self) -> None:
        cfg = SandboxConfig(gondolin_version="0.12.0")
        ensure_sidecar_installed(cfg, quiet=True)
        self.assertTrue(is_sidecar_installed(cfg))
