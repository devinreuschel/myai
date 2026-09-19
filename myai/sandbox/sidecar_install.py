"""Install and verify the Node Gondolin sidecar runtime."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from myai.sandbox.config import (
    DEFAULT_GONDOLIN_PACKAGE,
    DEFAULT_GONDOLIN_VERSION,
    SandboxConfig,
    gondolin_package_spec,
    sidecar_install_dir,
    sidecar_source_dir,
)

SIDECAR_SOURCES = ("sidecar.mjs", "guard.mjs")
LOCKFILE = "package-lock.json"


class SidecarError(Exception):
    pass


def sidecar_script_path() -> Path:
    """Path to the runnable sidecar script in the install cache."""
    return sidecar_install_dir() / "sidecar.mjs"


def _uses_shipped_lock(cfg: SandboxConfig) -> bool:
    """The bundled lockfile describes exactly one SDK: the default, pinned one."""
    return (
        cfg.gondolin_package == DEFAULT_GONDOLIN_PACKAGE
        and cfg.gondolin_version == DEFAULT_GONDOLIN_VERSION
    )


def is_sidecar_installed(cfg: SandboxConfig) -> bool:
    """Return True when the sidecar scripts and gondolin SDK are present."""
    install = sidecar_install_dir()
    gondolin = install / "node_modules" / "@earendil-works" / "gondolin"
    marker = install / ".installed-spec"
    if not all((install / name).is_file() for name in SIDECAR_SOURCES):
        return False
    if not gondolin.is_dir() or not marker.is_file():
        return False
    return marker.read_text(encoding="utf-8").strip() == gondolin_package_spec(cfg)


def _copy_if_changed(src: Path, dst: Path) -> None:
    if not dst.exists() or src.read_bytes() != dst.read_bytes():
        shutil.copy2(src, dst)


def ensure_sidecar_installed(cfg: SandboxConfig, *, quiet: bool = False) -> None:
    """Copy sidecar sources and npm-install the pinned Gondolin SDK.

    This is the one place myai runs npm on the host rather than in a VM, so it is
    kept tight: the package comes from validated global config only, lifecycle
    scripts never run, and the default SDK installs from a shipped lockfile so
    its whole dependency tree is pinned by hash.
    """
    cfg.validate()
    install = sidecar_install_dir()
    install.mkdir(parents=True, exist_ok=True)
    source = sidecar_source_dir()

    for name in SIDECAR_SOURCES:
        _copy_if_changed(source / name, install / name)

    if is_sidecar_installed(cfg):
        return

    spec = gondolin_package_spec(cfg)
    marker = install / ".installed-spec"
    marker.unlink(missing_ok=True)

    if _uses_shipped_lock(cfg):
        shutil.copy2(source / "package.json", install / "package.json")
        shutil.copy2(source / LOCKFILE, install / LOCKFILE)
        npm_cmd = "ci"
    else:
        # A user-chosen SDK has no lockfile of ours to install from.
        pkg = json.loads((source / "package.json").read_text(encoding="utf-8"))
        version = (
            cfg.gondolin_version
            if cfg.gondolin_version != "latest"
            else DEFAULT_GONDOLIN_VERSION
        )
        pkg["dependencies"] = {cfg.gondolin_package: version}
        (install / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
        (install / LOCKFILE).unlink(missing_ok=True)
        npm_cmd = "install"

    cmd = [
        "npm",
        npm_cmd,
        "--prefix",
        str(install),
        "--ignore-scripts",
        "--no-fund",
        "--no-audit",
    ]
    if quiet:
        cmd.append("--silent")
    result = subprocess.run(cmd, capture_output=quiet, text=True, check=False, timeout=600)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SidecarError(f"sidecar npm {npm_cmd} failed: {detail}")

    marker.write_text(spec + "\n", encoding="utf-8")
