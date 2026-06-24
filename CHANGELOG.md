# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `myai sandbox` command: run `pi` inside a Gondolin micro-VM, with `run`, `provision`, `doctor`, `ls`, `stop`, `snapshot`, `register`, and `init` subcommands
- Config via `.myai/sandbox.json` (repo) over `~/.myai/sandbox.json` (global), with `models.json` provisioning and `--tcp-map` host-loopback routes
- One-time provisioning phase (`sandbox provision` or auto on first `run`): installs pi to a host-mounted `/opt/pi` prefix (avoids guest rootfs `ENOSPC`) and pre-fetches fd/ripgrep into persistent host caches; allows npm/github only during provisioning, not at runtime
- Network policy: `network_policy` (`custom` default | `deny-all` | `allow-all`) / `--network`, `providers` / `--provider` (known LLM providers by name: `anthropic`, `openai`, `openrouter`, `gemini`, `github-copilot`, `github`, `ollama`, `llama.cpp`), and `allow_hosts` / `--allow-host`. Fail-closed: an empty allow list denies all egress; `allow-all` disables filtering and warns
- `host_secrets`: forward host env vars to the guest scoped to specific hosts, with optional `env_var` rename; values are injected via the child env, never the command line
- Host loopback to reach local host services from the guest: `host_loopback.enabled`, `--host-loopback`/`--no-host-loopback`, and `MYAI_HOST_LOOPBACK`/`MYAI_MODEL_ENDPOINT` env overrides
- `mirror_host_pi` / `--mirror-host-pi`: mirror host `~/.pi/agent` settings (packages, default provider/model) into the guest, rewriting localhost URLs to the loopback host; packages install during provisioning
- `llama_server_url`: passed to the guest as `LLAMA_SERVER_URL` (localhost rewritten to the loopback host) for the pi-llama-cpp extension
- `share_host_sessions` (default true): bind-mount host `~/.pi/agent/sessions` so host and guest `pi` share one session pool
- `guest_repo_mount` (`host_path` | `workspace`): mount the repo at its real host path (default, seamless cross-resume) or at `/workspace` (no path leak); workspace mode symlinks the `--workspace--` session slot to the repo's real dir so shared sessions still line up
- `auto_approve` (default true) / `--no-auto-approve`: pi auto-approves tool calls inside the sandbox; disable to require approval
- `--debug`: report executables the guest tried to run but couldn't find
- `rootfs_size` / `--rootfs-size` to grow the guest root disk (needs `e2fsprogs` in the image)
- Gondolin SDK sidecar (`myai/sandbox/sidecar/`): Python builds a JSON VM spec; Node drives `VM.create()` with programmable VFS and `vm.shell({ attach: true })`
- `guest_hidden_paths` (default `["/.myai"]`) and `--hide`: hide and deny workspace paths in the guest via `ShadowProvider`
- Sidecar npm install cache under `$MYAI_HOME/sandbox/sidecar/`; `doctor` checks npm + sidecar

### Changed

- **Breaking:** `host_loopback.enabled` defaults to `false` (cloud-first). Configs with a flat `model_endpoint` but no `host_loopback` section no longer emit `--tcp-map` or inject `--provider myai-local`; add `"host_loopback": { "enabled": true }` or pass `--model-endpoint` / `--host-loopback`.
- **Breaking:** runtime network is fail-closed. An empty allow list now denies all egress; allow hosts via `providers`/`allow_hosts` (+ loopback), or set `network_policy: "allow-all"` to opt out. github/npm stay allowed only in the provisioning VM.
- Updated sandbox rule injection so that guest `/root/.pi/agent/AGENTS.md` is written only for myai-managed repos that do not target pi, using the repo's selected rules from `.myai/config.json`. Unmanaged repos and pi-managed repos get no injected file (pi-managed repos rely on the synced repo `AGENTS.md` in the workspace mount).
- **Breaking:** sandbox no longer shells out to `npx @earendil-works/gondolin`; it uses the pinned SDK sidecar (`gondolin_version` default `0.12.0`)
- **Breaking:** removed warm VM reuse subcommands (`ls`, `stop`, `snapshot`, `register`) and `warm_reuse` config; each run cold-boots and exits with pi
- **Breaking:** `doctor` checks `npm` instead of `npx`

## [0.1.0] - 2026-06-21

### Added

- CLI package structure with `myai <command>` dispatch
- `install.sh` curl one-liner with platform-specific dependency detection
- Git clone install into `~/.local/share/myai` with isolated venv
- Idempotent re-run, uninstall path, and extensible install backend hooks
