# Roadmap

Scaffolding. The plan below is what we're building toward, in order.

### Phase 1: CLI foundation

Turn the project into a CLI you run as `myai <command>`.

- [x] Restructure from a single `main.py` into a `myai/` package
- [x] Wire up the `myai` entry point so it runs as a command
- [x] Command dispatch with per-subcommand modules

### Phase 2: End-user install

Install myai without PyPI via a curl one-liner.

- [x] `install.sh` curl one-liner (`curl -fsSL ... | sh`)
- [x] Detect git and Python 3.14+ before install
- [x] Show pre-flight summary of all machine changes; require user confirmation
- [x] Offer to install missing git/Python (platform-specific); continue on approval
- [x] Git clone install into `~/.local/share/myai`
- [x] Isolated venv + `pip install .` (no uv/pipx required)
- [x] Symlink `myai` into `~/.local/bin` and warn if not on PATH
- [x] Idempotent re-run (pull + reinstall)
- [x] Uninstall path (`install.sh --uninstall`)
- [x] Extensible install backends (`git` now; stub hooks for `pypi` / `release` later)

### Phase 2b: Shared agent rule layer

Central repo for rules, skills, and subagents synced to managed repos (cursor, claude, pi).

- [x] `myai/paths.py`: state root from `MYAI_HOME` / XDG
- [x] Global registry at `agentsync.json` (master path + managed repos)
- [x] `myai master init`: scaffold master repo dirs and register master
- [x] `myai init`: per-repo config (`.myai/config.json`) + overwrite warning
- [x] `myai sync`: render rules/skills/subagents to agent-native paths; tracked-only prune
- [x] `myai status`: master + repo drift summary
- [x] Renderers: cursor `.mdc`, claude `.claude/rules/*.md`, claude/pi managed blocks in `CLAUDE.md`/`AGENTS.md`, skill dirs
- [x] `nested_rules` toggle: nesting-capable agents (cursor, claude) emit nested rule files or flatten; pi always flattens
- [x] Claude subagent rendering (`.claude/agents/<name>.md`)
- [x] `myai global`: sync selected rules/skills/subagents into user-global agent homes (`~/.claude`, `~/.cursor`, `~/.pi/agent`)
- [ ] Reverse propagation (repo -> master)
- [ ] Auto `git pull` master before sync

### Phase 3: Prerequisite checks

Verify the host has what we need before we try to build anything, with clear messages when something's missing.

- [ ] Detect git
- [ ] Detect cmake and a working compiler

### Phase 4: llama.cpp build lifecycle

The core of the tool: own the clone, build, install, and update loop.

- [ ] Clone llama.cpp into the state dir
- [ ] Build from source
- [ ] Track latest release
- [ ] Pin to a specific version
- [ ] Build from upstream HEAD
- [ ] Cache builds per version and switch between them
- [ ] Expose built tools (`llama-cli`, `llama-server`, etc) on the path
- [ ] Check for and apply updates

### Phase 5: State dir & model store (file management)

Own the on-disk layout the rest of the tool reads and writes.

- [ ] `myai/paths.py`: resolve state root from `MYAI_HOME` then XDG (`~/.local/share/myai`), with `builds/`, `models/`, `cache/`, `run/`, `logs/`
- [ ] Content-addressed blob store at `models/blobs/sha256-<digest>` for GGUF files (dedup shared quants across tags)
- [ ] Manifest format: JSON at `models/manifests/<registry>/<name>/<tag>.json` mapping tag -> blob digests + params + template + source url
- [ ] `myai/store.py`: `resolve(ref) -> manifest`, `blob_path(digest)`, `add_blob()`, `link_tag()`, ref-counting across manifests
- [ ] `myai gc`: delete blobs no manifest references; `--dry-run` to preview
- [ ] Atomic writes: download/write to `*.partial`, `fsync`, then rename into place
- [ ] Cross-process file lock (`models/.lock`) around store mutations so concurrent pulls/runs don't corrupt state
- [ ] Disk accounting: `myai du` total + per-model size, surfaced in `myai list`
- [ ] Reuse llama.cpp's HF cache (`LLAMA_CACHE`) where it already has a blob, instead of re-downloading

### Phase 6: Model downloading

Pull GGUF models from Hugging Face (and later other registries) into the store.

- [ ] `myai pull <ref>`: download GGUF, verify, write blob + manifest, print final size/path
- [ ] HF ref parser in `myai/refs.py`: accept `user/repo`, `user/repo:Q4_K_M`, `hf.co/user/repo:quant`, bare `name:tag`
- [ ] Quant resolution: default to `Q4_K_M`, case-insensitive, fall back to first GGUF in repo, like llama.cpp's `-hf`
- [ ] `myai/download.py`: stdlib `urllib` downloader with HTTP Range resume, retry/backoff, and `sha256` verification
- [ ] Stdlib progress UI: bytes, percent, throughput, ETA on a single rewriting line
- [ ] Sharded GGUF support: detect `*-00001-of-000NN.gguf`, fetch all parts, register as one logical model
- [ ] `mmproj` companion download for multimodal repos (and `--no-mmproj` to skip)
- [ ] Gated/private repos: read `HF_TOKEN` env, send auth header, clear error on 401/403
- [ ] `MYAI_MODEL_ENDPOINT` override (HF-compatible mirrors), defaulting to huggingface.co
- [ ] `myai pull --quant <q>` and `myai pull --file <name.gguf>` to override resolution
- [ ] Stretch: ollama registry protocol (`registry.ollama.ai` manifest + blob pull) behind `ollama://` refs

### Phase 7: Model lifecycle commands

Lifecycle verbs over the store.

- [ ] `myai list` / `myai ls`: name:tag, quant, size, modified, short digest
- [ ] `myai/gguf.py`: parse GGUF header KV (arch, context length, quant type, param count, chat template) without loading weights
- [ ] `myai show <ref>`: arch, params, context, quant, license, template, source url
- [ ] `myai rm <ref>`: drop tag manifest, then GC now-orphaned blobs
- [ ] `myai cp <src> <dst>`: alias a manifest to a new name:tag (no blob copy)
- [ ] `myai import <file.gguf> <name:tag>`: ingest a local GGUF into the blob store
- [ ] `Myaifile` (our Modelfile): `FROM`, `SYSTEM`, `PARAM`, `TEMPLATE`; `myai create -f Myaifile <name>` materializes a manifest
- [ ] `myai ps`: loaded models, resident size, idle/keep-alive expiry (reads daemon state from Phase 10)

### Phase 8: Automatic llama.cpp management

Make the build invisible: right binary, right accel, no manual steps.

- [ ] Auto-build on first `run`/`serve` if no usable build is present (invoke Phase 4 lifecycle)
- [ ] Accelerator detection in `myai/hardware.py`: Metal (macOS), CUDA / ROCm / Vulkan / CPU (Linux) -> cmake flags
- [ ] Build cache keyed by `(version, accelerator)`; switch builds without rebuilding
- [ ] Auto `-ngl` (GPU layer count) from model size vs detected VRAM, with `--ngl` override
- [ ] `myai upgrade`: rebuild against newer llama.cpp release, keep old build until new one passes a smoke run
- [ ] Background update check (cached, throttled) that nudges when a newer release exists
- [ ] Build smoke test: run `llama-cli --version` + a 1-token generate before marking a build "good"

### Phase 9: Server initialization & passthrough

Spawn and supervise `llama-server`, with a raw passthrough escape hatch.

- [ ] `myai serve`: start the myai daemon (router from Phase 10) on a host:port, write pidfile to `run/`
- [ ] `myai run <ref>`: ensure model present (pull if missing), ensure a backend is up, drop into an interactive chat REPL
- [ ] Backend supervisor in `myai/backend.py`: spawn `llama-server -m <blob> <params>`, free-port allocation, `/health` poll before routing
- [ ] Map manifest params -> llama-server flags (`-c` context, `-np` parallel, `-ngl`, chat template, `--props`/`--slots` toggles)
- [ ] Stream child stdout/stderr to `logs/<model>.log`; surface load failures with the actual llama-server error
- [ ] `myai server -- <raw flags>`: passthrough that execs `llama-server` with user flags verbatim (bypasses the store)
- [ ] Lifecycle signals: graceful SIGTERM to children on `myai stop`, reap zombies, clean pidfiles on exit

### Phase 10: Model jukebox (on-demand swapping)

Many models on disk, one endpoint, load/swap/unload on demand.

- [ ] Router in `myai/router.py`: read `model` from each request, route to that model's backend, spawn it if cold
- [ ] On-demand load: first request for a model boots its backend and queues the request until `/health` is green
- [ ] Keep-alive: unload a model after idle timeout (`--keep-alive`, default 5m; `0` = unload immediately, `-1` = never), env `MYAI_KEEP_ALIVE`, per-request `keep_alive` override
- [ ] Resource budget: configurable max resident models + RAM/VRAM ceiling; LRU-evict to fit a new load
- [ ] Concurrency: per-model parallel request slots, global concurrent-model cap, request queue with backpressure
- [ ] Manual controls: `myai load <ref>`, `myai stop <ref>`, `myai unload --all`
- [ ] Pinned/preload models that boot on `serve` and never auto-unload
- [ ] Evaluate llama.cpp's native router mode (`--models-dir`, `POST /models/load|unload`) and either wrap it or run our own per-model `llama-server` processes

### Phase 11: HTTP API surface (drop-in compatibility)

The one place we want parity, so existing clients work unchanged.

- [ ] HTTP server in `myai/server/` (stdlib `http.server` or asyncio) fronting the jukebox
- [ ] OpenAI-compatible: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` (normalize/proxy to the routed llama-server)
- [ ] Ollama-native: `/api/chat`, `/api/generate`, `/api/embeddings`, `/api/tags`, `/api/show`, `/api/ps`, `/api/pull`, `/api/version`
- [ ] Streaming: SSE for `/v1/*`, newline-delimited JSON for `/api/*`
- [ ] Request mapping: ollama `options`/`keep_alive`/`format` and OpenAI params -> llama-server generation settings
- [ ] Default to ollama's port (`11434`) and honor `OLLAMA_HOST` so existing ollama clients point at us unchanged
- [ ] `myai api <path>`: curl-style debug helper against the local daemon
- [ ] Tool/function-calling and JSON-schema (`response_format`) passthrough where the model + llama-server support it

### Phase 12: Integration & exposure

Make myai a clean backend for agentic tools and remote use.

- [ ] `myai endpoint`: print base URL + API key for pasting into Cursor / Continue / aider / etc
- [ ] `myai env`: emit `OPENAI_BASE_URL` / `OPENAI_API_KEY` exports for the shell
- [ ] Network exposure: configurable bind host, optional API-key auth, CORS allowlist
- [ ] Embeddings endpoint usable by RAG tools (wired to llama-server `/v1/embeddings`)
- [ ] Service install: generate launchd (macOS) / systemd (Linux) units to run `myai serve` in the background
- [ ] Docs + recipes: "use myai as your OpenAI base URL" for the common agentic tools
- [ ] Stretch: tunnel/share helper for exposing the local endpoint to another machine

### Phase 13: Agentic teams — merge and rework the skeleton

Step one. The unmerged `agentic-teams` branch holds a board skeleton for the superseded pipeline design; land it and reshape it. Design: [docs/agentic-teams-design.md](agentic-teams-design.md). Phases 13–16 together are v1.

- [ ] Merge `agentic-teams` into `main`; resolve `CHANGELOG`, `cli.py`, `paths.py`, and `ROADMAP` conflicts, keeping these roadmap phases
- [ ] Keep: SQLite layer (`busy_timeout` before WAL, transactional migrations), `teams_*` path helpers, `$EDITOR` round-trip, the `teams` command group, PyYAML
- [ ] Replace the pipeline schema with a fresh `001`: `principals`, `contacts`, `conversations`, `participants`, `messages`, `message_recipients`, `tasks`, `mailbox`, `sessions`, `turns`, `events`
- [ ] N-participant conversations and `bot_id` on every row from day one (v1 runs one user, one bot)
- [ ] Move a pre-pivot `teams.db` aside on first open instead of migrating it
- [ ] Retire the `project` and `epic` commands, pipeline config validation, and role prompts; reuse the YAML validation pattern for `bot.yaml`
- [ ] Rework `task` and `status` onto the new schema (a task is a thread with an owner and a status)
- [ ] State under `state_root()/teams/` with `bots/` and `artifacts/` (no `worktrees/`); user settings in `~/.myai/teams.json`; no absolute client paths in state
- [ ] Rework `tests/test_teams.py`: keep DB, migration, editor, and CLI-capture tests; drop epic and pipeline tests
- [ ] README and CHANGELOG describe the reworked `teams` surface instead of the board

### Phase 14: Agentic teams — daemon, protocol, and one bot

Foundation for long-lived bots: a daemon, a client protocol, and the owned agent loop.

- [ ] `teams daemon start|stop|status`: detached process; single instance via `flock`; epoch per start
- [ ] Client protocol: JSON over HTTP on a unix socket; SSE event stream with `Last-Event-ID` replay
- [ ] Exactly-once state changes: mailbox item consumed and its effects committed in one transaction
- [ ] Bot home dir: `bot.yaml`, `persona.md`, `constraints.md`, `playbooks/`; `teams bot new|edit|list`
- [ ] Provider clients: OpenAI-compatible (OpenAI, OpenRouter, llama.cpp) and native Anthropic; API keys daemon-side only
- [ ] Route config per role (single entry in v1); per-bot model, effort/thinking, and sampling settings
- [ ] Owned agent loop running daemon-side; every turn boundary persisted before the next model call
- [ ] Environment boundary interface (exec, files, git) with the plain-directory implementation; no tool bypasses it
- [ ] Policy checkpoint on every side-effecting tool call (v1: allow + log); `policy_decisions` recorded
- [ ] Wake lifecycle: mailbox priority (user, job event, bot message, schedule); one wake at a time; mid-wake messages queue
- [ ] Wake turn budget with a delegate-or-wrap-up nudge
- [ ] Context assembly: stable prefix, semi-stable block, volatile tail; append-only within a session
- [ ] TUI client: DM with one bot, queued-message indicator, reconnect and replay
- [ ] `teams send`, `teams inbox`, `teams approve|reject` for scripting

### Phase 15: Agentic teams — memory

Long-lived without getting stupid: mined facts, not repeated summaries.

- [ ] Episodic log: immutable `turns` with harness-set `origin` (`first_hand`|`recalled`) and `event_time`
- [ ] Sessions resumable from the last durable turn boundary; interrupted tool calls flagged on resume
- [ ] Context-full strategy interface; `rollover` implementation (handoff note, mine, fresh session)
- [ ] Survival records for every rollover; rollover markers in the chat
- [ ] Tasks with per-task handoff notes pushed at the next wake
- [ ] Librarian: post-session mining into self-contained, scoped facts with source turns and how-known
- [ ] Provenance rules enforced: first-hand grounding only; recalled spans readable but never a source; bot messages never a source
- [ ] Reconcile by event time: add / supersede / expire / no-op; serialized per scope; idempotent re-mining
- [ ] `facts` + FTS5; `scope` column (private only in v1); `mined_by` model recorded
- [ ] User edits and reverts of facts recorded as events so a rebuild replays them
- [ ] Retrieval pushed at wake: verbatim conversation tail + search keyed on the incoming message, with age and how-known inline
- [ ] Pull tools: `memory.search`, `memory.open`, `memory.source`, `history.search`, `history.read` (own conversations only)
- [ ] Hierarchical memory index in the prefix (topics, facts, sources)
- [ ] Context manifest per reply (turns, facts, playbooks, handoff, constraints, model, route, tokens)
- [ ] Debrief proposals: playbook edits and constraint promotions, applied or queued per preset
- [ ] Budgeted `constraints.md` with forced merge/demote when full
- [ ] Block presets (`hands-off`, `supervised`, `locked-down`) over `auto|notify|gate`; per-bot overrides
- [ ] Display toggle for manifests, mining diffs, and survival records; capture always on
- [ ] Optional local llama.cpp route for mining (user setting)
- [ ] Replay harness: teach in episode 1, probe in episode N across rollovers and mining; manifest-level assertions

### Phase 16: Agentic teams — jobs, workers, and workspaces

A bot stays free; long work runs in sub-bots or Cursor cloud agents.

- [ ] Executor seam: `spawn`, `status`, `steer`, `cancel`, `result`; job events into the owner's mailbox
- [ ] `self` executor: sub-bot on the owned loop with a context package; depth 1; reports only to the parent; `done|failed|needs_input`
- [ ] Sub-bot gets its own workspace directory; one `self` job per bot at a time
- [ ] Sub-bot transcripts mined into the parent's memory
- [ ] Open-jobs summary in every parent wake; steer or cancel decided by the bot from message content
- [ ] Cursor connector: create agent, follow-up, cancel, SSE stream, usage; completion by stream or poll; key daemon-side
- [ ] Reattach to external jobs by `external_ref` after a daemon restart
- [ ] Bot-owned clones under its workspace; delivery by branches and PRs; never the user's checkout, local included
- [ ] Forge credential held daemon-side for pushes
- [ ] Repo `AGENTS.md` loaded as project-scoped constraints when working in that repo
- [ ] `transfer` tool: client pushes, always gated, exact paths and sizes shown, secret-looking paths refused, waits in the inbox with no client attached
- [ ] Artifact delivery from bot to user (`notify`)
- [ ] Always-gated classes enforced from v1: irreversible external actions and `transfer`

### Phase 17: Agentic teams — multi-bot and teams

Slack-shaped: DMs, groups, owned tasks, shared knowledge.

- [ ] Multiple bots; bot-to-bot DMs; user-created group chats
- [ ] Addressed wakes: membership = visibility, recipients = wake
- [ ] Contacts enforced as an ACL on every send
- [ ] Task assignment with the `assign` permission; board view over tasks
- [ ] Team: roster, shared-memory scope, policy defaults; default template with a lead
- [ ] Lead as default recipient for unaddressed group messages; no lead wakes all members
- [ ] Team-shared memory: gated promotion from private; write-to-shared as a per-bot permission
- [ ] Schedules and external triggers as wake sources
- [ ] Policy rules: deny / require-approval on tool calls, with teaching denial messages
- [ ] Action-triggered constraint injection keyed on the pending call
- [ ] Mined constraints routed to policy rule, system prompt, or trigger

### Phase 18: Agentic teams — isolation tiers

Each bot gets its own environment, connectors, and egress.

- [ ] Research: viable isolation tier on VPSes without `/dev/kvm`; Node/QEMU footprint on the box
- [ ] Gondolin microVM environment via `myai/sandbox` (non-interactive exec, per-bot VM spec)
- [ ] Container environment tier
- [ ] Per-bot egress allowlist and connector set enforced by the environment
- [ ] Credentials applied at the boundary; never in environment files or process env
- [ ] Hydrate on wake, tear down on idle; workspace persisted outside the environment
- [ ] MCP server placement decided (daemon-side proxy vs in-environment)
- [ ] Reduced-permission sub-bots (authority only narrows downward)

### Phase 19: Agentic teams — BYO cloud and model routes

Close the laptop; the bots keep working on your own box.

- [ ] `myai cloud bootstrap user@host`: run `install.sh` over SSH, systemd user unit, `enable-linger`, health check, save host
- [ ] Interpreter strategy for stock VPS images (Python 3.14)
- [ ] Client transport: `ssh` subprocess forwarding the daemon socket; no open ports
- [ ] Multiple clients attached at once; reconnect with cursor replay across sleeps
- [ ] Route engine: ordered endpoints, health checks, `on_unavailable: fallback|wait|fail`
- [ ] Fallback opt-in per bot; config-time warning for local-only always-on roles; route health in bot status
- [ ] Park and resume wakes on route loss; deferred mining queue when the miner route is down
- [ ] Reverse-forward a client's `llama-server` to the box; last hop via the `model.host` loopback route
- [ ] Endpoint capability declarations (tool calling, context length)
- [ ] State directory backup and restore
- [ ] Re-run bootstrap as the upgrade path; uninstall

### Phase 20: Agentic teams — more clients

Same protocol, more surfaces.

- [ ] Chat bridge client (conversation mapped onto an external chat app) with push notifications
- [ ] Inbox items and gates resolvable from the bridge
- [ ] Web client on the same protocol (desktop, browser, mobile)

### Phase 21: Agentic teams — later options

- [ ] Vector search behind the same retrieval interface, once FTS recall demonstrably fails
- [ ] `compact` context-full strategy emitting the same survival record
- [ ] Claude Code binary as an executor (unmodified binary, the user's own login)
- [ ] pi as an executor, run through the existing `myai sandbox`
- [ ] Explicit in-session `remember` tool, if the miner alone proves insufficient
- [ ] Cost controls: mailbox throttling and budgets from captured usage
