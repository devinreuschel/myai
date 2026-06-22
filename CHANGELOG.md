# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `myai sandbox` command: run `pi` inside a Gondolin micro-VM, with `run`, `provision`, `doctor`, `ls`, `stop`, `snapshot`, `register`, and `init` subcommands
- Config via `.myai/sandbox.json` (repo) over `~/.myai/sandbox.json` (global), with `models.json` provisioning and `--tcp-map` host-loopback routes
- One-time provisioning phase (`sandbox provision` or auto on first `run`): installs pi and pre-fetches fd/ripgrep into persistent host caches; allows npm/github only during provisioning, not at runtime
- Host loopback to reach local host services from the guest: `host_loopback.enabled`, `--host-loopback`/`--no-host-loopback`, and `MYAI_HOST_LOOPBACK`/`MYAI_MODEL_ENDPOINT` env overrides
- `mirror_host_pi` / `--mirror-host-pi`: mirror host `~/.pi/agent` settings (packages, default provider/model) into the guest, rewriting localhost URLs to the loopback host; packages install during provisioning
- `llama_server_url`: passed to the guest as `LLAMA_SERVER_URL` (localhost rewritten to the loopback host) for the pi-llama-cpp extension
- `share_host_sessions` (default true): bind-mount host `~/.pi/agent/sessions` so host and guest `pi` share one session pool
- `guest_repo_mount` (`host_path` | `workspace`): mount the repo at its real host path (default, seamless cross-resume) or at `/workspace` (no path leak)
- `rootfs_size` / `--rootfs-size` to grow the guest root disk (needs `e2fsprogs` in the image)

### Fixed

- Guest `ENOSPC` on first-boot `npm install`: pi installs to a host-mounted prefix at `/opt/pi` instead of the small alpine-base rootfs

### Changed

- **Breaking:** `host_loopback.enabled` defaults to `false` (cloud-first). Configs with a flat `model_endpoint` but no `host_loopback` section no longer emit `--tcp-map` or inject `--provider myai-local`; add `"host_loopback": { "enabled": true }` or pass `--model-endpoint` / `--host-loopback`.
- **Breaking:** github/npm are no longer auto-allowed during interactive `sandbox run`. They are allowed only in the one-time provisioning VM (`sandbox provision`). Runtime honors `allow_hosts` (+ loopback) only; add github to `allow_hosts` explicitly if the agent needs it at runtime.

## [0.1.0] - 2026-06-21

### Added

- CLI package structure with `myai <command>` dispatch
- `install.sh` curl one-liner with platform-specific dependency detection
- Git clone install into `~/.local/share/myai` with isolated venv
- Idempotent re-run, uninstall path, and extensible install backend hooks
