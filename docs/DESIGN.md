# Design notes

## Agent rule sync

Rules live in a master repo and sync to managed repos via `myai sync`. Each repo
declares which agents it manages in `.myai/config.json` (`cursor`, `claude`, `pi`).

### Nested vs flat rules

`nested_rules` (default `true`) controls how nesting-capable agents get rules:

| Agent | Nested (`nested_rules: true`) | Flat (`nested_rules: false`) |
|-------|--------------------------------|------------------------------|
| cursor | `.cursor/rules/<name>.mdc` | managed block in `AGENTS.md` |
| claude | `.claude/rules/<name>.md` | managed block in `CLAUDE.md` |
| pi | (not supported) | managed block in `AGENTS.md` |

Pi is flat-only. When cursor and pi both flatten, they share one `AGENTS.md`
block (rules merged, deduped by name). When cursor and pi both run with nested
rules, cursor gets nested files and pi gets `AGENTS.md` (the "wasteful" case).

Set at init with `--flat-rules` or edit `.myai/config.json` directly.

### Per-agent capabilities

Capabilities are defined in `myai/agentsync/render.py` (`AGENT_CAPS`):

- **cursor**: nested `.mdc` files with `globs`/`alwaysApply` frontmatter; skills at `.cursor/skills/`
- **claude**: nested `.md` files under `.claude/rules/` (Claude Code discovers them recursively); skills at `.claude/skills/`; subagents at `.claude/agents/`
- **pi**: flat-only; skills at `.pi/skills/`

Rules can be scoped per-agent via frontmatter `agents: [cursor, claude]`.

### myai-managed guardrail

Managed repos can inject a guardrail that tells agents not to edit myai-managed rules, skills, or subagents directly and to guide the user to change the master repo instead.

Delivery differs by agent (cursor and claude have no synced system-prompt file):

| Agent | Nested rules (`nested_rules: true`) | Flat rules |
|-------|-------------------------------------|------------|
| cursor | `.cursor/rules/myai-managed.mdc` (`alwaysApply: true`) | `## myai-managed` in `AGENTS.md` managed block |
| claude | `.claude/rules/myai-managed.md` | `## myai-managed` in `CLAUDE.md` managed block |
| pi | `.pi/APPEND_SYSTEM.md` (appended to system prompt, not a replacement) | same |

Toggle resolution (default on):

- Global default: `inject_myai_rule` in `~/.myai/config.json` (absent = true).
  Set with `myai config myai-rule on|off`. Legacy values in `agentsync.json` are read once until the global config file exists or is written.
- Per-repo override: `inject_myai_rule` in `.myai/config.json` (`null`/absent = inherit global). Set at init with `--no-myai-rule`.

| Repo state | cursor / claude | pi |
|------------|-----------------|-----|
| Not myai-managed | Nowhere | Nowhere |
| Managed, toggle off | Nowhere | Nowhere |
| Managed, toggle on | Synced rule (nested or flat) | `.pi/APPEND_SYSTEM.md` via `myai sync` |
| Managed, toggle on, no `pi` in `agents` (VM only) | — | `/root/.pi/agent/APPEND_SYSTEM.md` at VM boot |

When the repo already syncs for pi, the VM does not inject its own copy; the synced project file is picked up from the workspace mount.

### Sandbox rule injection

When running `myai sandbox run`, guest `pi` reads global instructions from
`/root/.pi/agent/AGENTS.md` (via `PI_CODING_AGENT_DIR`). Injection is gated on
the repo's `.myai/config.json`:

| Repo state | Injected `/root/.pi/agent/AGENTS.md` | Injected `/root/.pi/agent/APPEND_SYSTEM.md` |
|------------|--------------------------------------|---------------------------------------------|
| Not myai-managed (no config) | No | No |
| Managed, toggle off | No | No |
| Managed, `pi` in `agents` | No — use synced repo `AGENTS.md` in the workspace | No — use synced `.pi/APPEND_SYSTEM.md` |
| Managed, no `pi` in `agents` | Yes — repo's selected rules, filtered for pi | Yes — myai-managed guardrail |

This lets cursor-only (or claude-only) managed repos run sandboxed `pi` with the
same rule set without duplicating rules when the repo already syncs for pi.

## Agent sandbox: phasing and escape hatches

We run interactive `pi` inside a microVM so it feels like plain `pi` but the
process is hardware-isolated (own kernel, mediated network, secrets off the
guest). Constraints: Python-drivable, local/self-hosted with no third-party
account, real microVM isolation, and a true bidirectional PTY (raw stdin,
stdout/stderr, resize). The PTY requirement is the decisive criterion and rules
out most exec-only sandbox SDKs.

### Phase 1 (done): Gondolin CLI wrapper

Python built `gondolin bash` argv with `--mount-hostfs`, `--allow-host`, etc.
Whole-repo bind mounts exposed project `.myai/` to the guest.

### Phase 2 (done): Node sidecar + SDK VFS

Implemented in `myai/sandbox/sidecar/`. Python builds a JSON VM spec; a small
Node sidecar imports `@earendil-works/gondolin`, calls `VM.create()` with
programmable VFS mounts, and runs `vm.shell({ attach: true })` for the
interactive pi session.

The workspace mount uses `ShadowProvider` + `createShadowPathPredicate` to hide
and deny guest access to configured paths (default: `/.myai`). Host-side sandbox
config (`.myai/sandbox.json`) is read only on the host before boot; the guest
never needs the repo-level `.myai/` directory.

Each `sandbox run` cold-boots a fresh VM that exits when pi exits. Pi session
history still persists via the host-backed `~/.pi/agent/sessions` mount
(`share_host_sessions`, default true), so `pi --resume <id>` works on the host or
via `myai sandbox run -- --resume <id>`.

### Phase 3 (if the Node dependency itself is the problem): Python-native swap

Triggered when the friction is "we don't want Node in a Python product" rather
than a missing Gondolin feature. Drop Gondolin for a microVM runtime with a
first-class Python SDK. **Gate: validate the candidate's PTY can carry a full
interactive `pi` session (attach, type, resize) before committing.** If it
can't, stay on Phase 1/2.

Options, ranked by fit:

- **microsandbox (primary).** Closest Python-native analog to Gondolin:
  local-first libkrun microVMs (~sub-100ms boot), embeddable Python SDK via pyo3
  with runtime binaries shipped in the wheel (no daemon/server/account),
  network-layer secret injection + network/DNS/TLS policy, runs OCI images,
  Apache-2.0. Risks: PTY maturity must be verified (docs lean on
  `exec`/`exec_stream`), pre-1.0, Linux+KVM or macOS Apple Silicon only.
- **E2B self-hosted (proven-PTY fallback).** Firecracker microVMs, mature Python
  SDK, first-class proven PTY, Apache-2.0. Downside: cloud-first; self-hosting
  means standing up their orchestrator (heavier ops than `pip install`).
- **Arrakis** cloud-hypervisor microVMs, self-hosted, REST + Python
  SDK, snapshot/restore (useful for rewind/MCTS agent flows). Downside:
  run-a-REST-server model and a VNC-leaning interactive story, a poor fit for a
  transparent terminal TUI.

Excluded by constraints: Modal (gVisor, cloud-only), Daytona (container/cloud),
Docker `sbx` (requires Docker account/login). DIY (drive libkrun/Firecracker
directly) means rebuilding the guest agent + PTY channel; not worth it unless a
hard requirement blocks every option above.
