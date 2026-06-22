"""Integration tests requiring gondolin, qemu, and a running model server."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("npx"), "npx not installed")
@unittest.skipUnless(os.environ.get("MYAI_SANDBOX_INTEGRATION"), "set MYAI_SANDBOX_INTEGRATION=1 to run")
class SandboxIntegrationTests(unittest.TestCase):
    def test_gondolin_list(self) -> None:
        out = subprocess.run(
            ["npx", "--yes", "@earendil-works/gondolin", "list"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
