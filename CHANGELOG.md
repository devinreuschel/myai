# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `myai teams` state layer for long-lived bots: SQLite store under XDG state with N-participant conversations, addressed messages, per-bot mailboxes, task threads, and an append-only event log; `init`, `bot new|edit|list`, `task add|edit|list|show`, `status`. Nothing executes until the daemon lands
- Bot homes at `teams/bots/<id>/` (`bot.yaml`, `persona.md`, `constraints.md`, `playbooks/`, `workspace/`); `bot.yaml` is validated on edit and written back verbatim
- A bot can only address its contacts, and only addressed bots are queued a wake; `turns` and `events` rows cannot be updated or deleted
- A `teams.db` from the earlier pipeline prototype is moved aside (`teams.db.pre-pivot-<timestamp>`) rather than migrated
- PyYAML dependency for bot config and task frontmatter round-trips
- `myai global init|sync|status`: sync selected master rules/skills/subagents into user-global agent homes (`~/.claude`, `~/.cursor`, `~/.pi/agent`), with selection in `~/.myai/global.json` and prune state under XDG; independent of per-repo `myai sync`. Cursor global rules are unsupported (skills only). Sync refuses to overwrite untracked existing home files unless the user confirms or passes `-y`.
- Init selection flags (`--agent`, `--rule`, `--skill`, `--subagent`) on `myai init` and `myai global init` accept comma-separated values (`--rule a,b`) in addition to repeated flags; `all` selects the full master catalog for that kind (rules/skills/subagents store `"all"` and resolve on each sync; agents expand at init)
- myai-managed guardrail: when enabled (default), injects instructions not to edit synced rules/skills/subagents directly; cursor/claude get an always-apply rule (nested file or managed block), pi gets `.pi/APPEND_SYSTEM.md` via `myai sync`, non-pi managed repos also get `/root/.pi/agent/APPEND_SYSTEM.md` at VM boot
- Global user settings in `~/.myai/config.json` (alongside `~/.myai/sandbox.json`); `inject_myai_rule` lives here
- `myai config myai-rule [on|off]` to show or set the global default for the myai-managed guardrail
- `myai init --no-myai-rule` to disable the guardrail for a repo (overrides the global default)
- Per-repo `inject_myai_rule` in `.myai/config.json` (`null`/absent = inherit global default)
- `myai sandbox` command: run `pi` inside a Gondolin micro-VM, with `run`, `provision`, `doctor`, `ls`, `stop`, `snapshot`, `register`, and `init` subcommands
- Config via `.myai/sandbox.json` (repo) over `~/.myai/sandbox.json` (global), with `models.json` provisioning and `--tcp-map` host-loopback routes
- One-time provisioning phase (`sandbox provision` or auto on first `run`): installs pi to a host-mounted `/opt/pi` prefix (avoids guest rootfs `ENOSPC`) and pre-fetches fd/ripgrep into persistent host caches; allows npm/github only during provisioning, not at runtime
- Network policy: `network_policy` (`custom` default | `deny-all` | `allow-all`) / `--network`, `providers` / `--provider` (known LLM providers by name: `anthropic`, `openai`, `openrouter`, `gemini`, `github-copilot`, `github`, `ollama`, `llama.cpp`), and `allow_hosts` / `--allow-host`. Fail-closed: an empty allow list denies all egress; `allow-all` disables filtering and warns
- `host_secrets`: forward host env vars to the guest scoped to specific hosts, with optional `env_var` rename; values are injected via the child env, never the command line
- Host loopback to reach local host services from the guest: `host_loopback.enabled`, `--host-loopback`/`--no-host-loopback`, and `MYAI_HOST_LOOPBACK`/`MYAI_MODEL_ENDPOINT` env overrides
- `mirror_host_pi` / `--mirror-host-pi`: mirror host `~/.pi/agent` settings (packages, default provider/model) into the guest, rewriting localhost URLs to the loopback host; packages install during provisioning
- `llama_server_url`: passed to the guest as `LLAMA_SERVER_URL` (localhost rewritten to the loopback host) for the pi-llama-cpp extension
- `share_host_sessions` (default true): share this repo's slot of host `~/.pi/agent/sessions` with the guest so host and guest `pi` resume each other's sessions; other projects' transcripts are not mounted
- `guest_repo_mount` (`host_path` | `workspace`): mount the repo at its real host path (default, seamless cross-resume) or at `/workspace` (no path leak); workspace mode mounts the repo's real session slot as `--workspace--` so shared sessions still line up
- `auto_approve` (default true) / `--no-auto-approve`: pi auto-approves tool calls inside the sandbox; disable to require approval
- `--debug`: report executables the guest tried to run but couldn't find
- `rootfs_size` / `--rootfs-size` to grow the guest root disk (needs `e2fsprogs` in the image)
- Gondolin SDK sidecar (`myai/sandbox/sidecar/`): Python builds a JSON VM spec; Node drives `VM.create()` with programmable VFS and `vm.shell({ attach: true })`
- `guest_hidden_paths` and `--hide`: hide and deny extra workspace paths in the guest; `/.myai` is always hidden and `--hide` adds to the list
- Sidecar npm install cache under `$MYAI_HOME/sandbox/sidecar/`; `doctor` checks npm + sidecar

### Added

- `git_access` sandbox setting (`read-only` default | `commit` | `write`) with `--git-commit` / `--git-write`. In `commit` mode the agent commits into a scratch clone that borrows the repo's objects read-only; when the run ends myai imports the agent's branches into `refs/sandbox/<run>/*` on the real repo (fsck'd, hooks disabled) for pull-request-style review, and the real `.git` never takes a write from inside the sandbox
- The sandbox now provisions a working `git` into the guest (the base image has none): installed during provisioning and staged into the persistent cache — binary in `pi-bin`, helpers and libraries in `git-bundle/` — so runs have local git (`status`/`diff`/`log`/`commit`/`branch`/`rebase`) with no runtime network

### Security

- `install.sh` cloned from a placeholder GitHub owner (`OWNER`) that a third party controls; it now clones this repo
- `install.sh` installs into `~/.local/share/myai-app` instead of myai's state dir, and `--uninstall` confirms, refuses anything that is not a myai clone, leaves a `myai` binary it did not create, and never touches your data
- `myai sync` no longer trusts a repo's `.myai/state.json` or working tree: paths must be canonical, inside the repo after resolving symlinks, and (for deletes) inside the directories sync manages. Unsafe state entries are dropped with a warning; a write target that leaves the repo fails the sync before anything is written; a symlink at a destination is replaced, not written through. `CLAUDE.md -> AGENTS.md` style links inside the repo still work
- **Breaking:** a repo's `.myai/sandbox.json` is ignored until approved with `myai sandbox trust`, which shows what the config grants (hosts, secrets and where they go, host ports, ssh, mounts); any edit needs approving again. `myai sandbox init` approves what it writes and no longer overwrites an existing file without `--force`. `--ignore-repo-config` runs on the global config alone
- **Breaking:** `gondolin_package`, `gondolin_version`, and `pi_package` are global-only; a repo's values are ignored with a warning. They must be a plain npm name and an exact version (no URLs, ranges, or git refs)
- Host-side sidecar install uses `npm ci --ignore-scripts` from a shipped lockfile, so the SDK's dependency tree is pinned by hash and no lifecycle script runs on the host
- **Breaking:** `.git` is read-only in the guest by default (`git_access: read-only`): hooks and config written there would run on the host. The agent can still read history and diff, but not commit — use `git_access: commit` for committing without exposing the real `.git`, or `--git-write` for the direct-write escape hatch
- The workspace guard matches names case-insensitively and resolves symlinked parents, closing two ways around hidden paths on macOS (`.MYAI/…`, and creating a new file through `ln -s .myai x`)
- pi's install, tools, and package caches are read-only during runs; only the provisioning VM can write them
- Session-slot symlinks left by the guest are removed after each run
- `allow_hosts`, `ssh_allow_hosts`, and secret hosts are validated; `*` is only accepted as a leading label over at least two more (`*.example.com`). Secret names must be env-var shaped and cannot shadow `PATH`, `NODE_OPTIONS`, `LD_PRELOAD`, and the like
- `allow-all` keeps secrets as per-host placeholders instead of handing the guest their real values
- **Breaking:** `deny-all` also turns off host loopback routes. Loopback upstreams must be this machine unless `host_loopback.allow_remote_upstreams` / `--allow-remote-upstream` is set; link-local (cloud metadata) addresses are never bridged
- `sandbox run` refuses a directory that contains your home, `$MYAI_HOME`, or `~/.myai`
- `pi_package` is shell-quoted in the provisioning script

### Changed

- A repo `sandbox.json` now overrides only the keys it names; a sparse file no longer resets your global choices (such as `auto_approve: false`) to defaults
- Global `inject_myai_rule` moved from `~/.local/share/myai/agentsync.json` to `~/.myai/config.json`; legacy registry values are read until the global config file exists or is written via `myai config myai-rule`
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
