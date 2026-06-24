"""Install and verify the Node Gondolin sidecar runtime."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from myai.sandbox.config import SandboxConfig, gondolin_package_spec, sidecar_install_dir, sidecar_source_dir


class SidecarError(Exception):
    pass


def sidecar_script_path() -> Path:
    """Path to the runnable sidecar script in the install cache."""
    return sidecar_install_dir() / "sidecar.mjs"


def is_sidecar_installed(cfg: SandboxConfig) -> bool:
    """Return True when the sidecar script and gondolin SDK are present."""
    install = sidecar_install_dir()
    script = install / "sidecar.mjs"
    gondolin = install / "node_modules" / "@earendil-works" / "gondolin"
    marker = install / ".installed-spec"
    if not script.is_file() or not gondolin.is_dir():
        return False
    if not marker.is_file():
        return False
    return marker.read_text(encoding="utf-8").strip() == gondolin_package_spec(cfg)


def ensure_sidecar_installed(cfg: SandboxConfig, *, quiet: bool = False) -> None:
    """Copy sidecar sources and npm-install the pinned Gondolin SDK."""
    install = sidecar_install_dir()
    install.mkdir(parents=True, exist_ok=True)
    source = sidecar_source_dir()

    for name in ("sidecar.mjs", "package.json"):
        src = source / name
        dst = install / name
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            shutil.copy2(src, dst)

    spec = gondolin_package_spec(cfg)
    if is_sidecar_installed(cfg):
        return

    pkg = json.loads((install / "package.json").read_text(encoding="utf-8"))
    version = cfg.gondolin_version if cfg.gondolin_version != "latest" else "0.12.0"
    pkg["dependencies"] = {cfg.gondolin_package: version}
    (install / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")

    cmd = ["npm", "install", "--prefix", str(install), "--no-fund", "--no-audit"]
    if quiet:
        cmd.append("--silent")
    result = subprocess.run(cmd, capture_output=quiet, text=True, check=False, timeout=600)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SidecarError(f"sidecar npm install failed: {detail}")

    (install / ".installed-spec").write_text(spec + "\n", encoding="utf-8")
