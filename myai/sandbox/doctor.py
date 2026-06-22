import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


MIN_NODE_MAJOR = 23
MIN_NODE_MINOR = 6
MIN_DISK_GB = 5


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def run_doctor() -> list[CheckResult]:
    return [
        _check_node(),
        _check_npx(),
        _check_qemu(),
        _check_krun(),
        _check_virtualization(),
        _check_gondolin_cache(),
        _check_disk_space(),
    ]


def doctor_ok(results: list[CheckResult]) -> bool:
    required = {"node", "npx", "qemu", "virtualization", "disk"}
    for result in results:
        if result.name in required and not result.ok:
            return False
    return True


def print_doctor(results: list[CheckResult]) -> None:
    for result in results:
        mark = "ok" if result.ok else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
        if not result.ok and result.fix:
            print(f"      fix: {result.fix}")


def _check_node() -> CheckResult:
    node = shutil.which("node")
    if not node:
        return CheckResult(
            "node",
            False,
            "node not found",
            "install Node.js >= 23.6 (https://nodejs.org or nvm)",
        )
    try:
        out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        version = out.stdout.strip().lstrip("v")
        major, minor, *_ = (int(p) for p in version.split("."))
        if (major, minor) < (MIN_NODE_MAJOR, MIN_NODE_MINOR):
            return CheckResult(
                "node",
                False,
                f"found {version}, need >= {MIN_NODE_MAJOR}.{MIN_NODE_MINOR}",
                "upgrade Node.js",
            )
        return CheckResult("node", True, version)
    except Exception as exc:
        return CheckResult("node", False, str(exc), "install Node.js >= 23.6")


def _check_npx() -> CheckResult:
    npx = shutil.which("npx")
    if not npx:
        return CheckResult("npx", False, "npx not found", "install Node.js/npm (npx ships with npm)")
    return CheckResult("npx", True, npx)


def _qemu_binary() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "qemu-system-aarch64"
    return "qemu-system-x86_64"


def _check_qemu() -> CheckResult:
    binary = _qemu_binary()
    path = shutil.which(binary)
    if not path:
        system = platform.system()
        fix = "brew install qemu" if system == "Darwin" else "apt install qemu-system-x86 (or distro equivalent)"
        return CheckResult("qemu", False, f"{binary} not found", fix)
    return CheckResult("qemu", True, path)


def _check_krun() -> CheckResult:
    path = shutil.which("krun")
    if not path:
        return CheckResult(
            "krun",
            False,
            "not installed (optional; QEMU is the default backend)",
            "install libkrun for faster Apple Silicon VMs, or use --vmm qemu",
        )
    return CheckResult("krun", True, path)


def _check_virtualization() -> CheckResult:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        kvm = Path("/dev/kvm")
        if kvm.exists():
            return CheckResult("virtualization", True, "/dev/kvm present")
        return CheckResult(
            "virtualization",
            False,
            "/dev/kvm missing",
            "enable KVM in BIOS and load kvm module, or use software emulation (slower)",
        )
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return CheckResult(
            "virtualization",
            True,
            "Apple Silicon (Hypervisor.framework available for krun/QEMU)",
        )
    if system == "Darwin":
        return CheckResult("virtualization", True, "macOS Intel (QEMU software emulation)")
    return CheckResult("virtualization", True, f"{system} {machine}")


def _check_gondolin_cache() -> CheckResult:
    cache = Path.home() / ".cache" / "gondolin" / "images"
    sessions = Path.home() / ".cache" / "gondolin" / "sessions"
    if cache.is_dir() and any(cache.iterdir()):
        return CheckResult("gondolin_assets", True, f"cached images in {cache}")
    return CheckResult(
        "gondolin_assets",
        True,
        f"no cached images yet (first run downloads ~200MB to {cache})",
    )


def _check_disk_space() -> CheckResult:
    root = Path.home()
    try:
        usage = shutil.disk_usage(root)
        free_gb = usage.free / (1024**3)
        if free_gb < MIN_DISK_GB:
            return CheckResult(
                "disk",
                False,
                f"{free_gb:.1f} GiB free under {root}",
                f"need at least {MIN_DISK_GB} GiB for guest assets and checkpoints",
            )
        return CheckResult("disk", True, f"{free_gb:.1f} GiB free")
    except Exception as exc:
        return CheckResult("disk", False, str(exc))


def failure_message(results: list[CheckResult]) -> str:
    lines = ["sandbox prerequisites not met:"]
    for result in results:
        if not result.ok and result.name in {"node", "npx", "qemu", "virtualization", "disk"}:
            lines.append(f"  - {result.name}: {result.detail}")
            if result.fix:
                lines.append(f"    {result.fix}")
    lines.append("")
    lines.append(
        "If your host cannot run micro-VMs, pi also ships an intercept-mode "
        "Gondolin extension (tool routing on the host) in the pi monorepo."
    )
    lines.append("Run: myai sandbox doctor")
    return "\n".join(lines)
