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

### Phase 13: Agentic teams — skeleton

Project/epic/task board over SQLite. Design: [docs/agentic-teams-design.md](agentic-teams-design.md). No daemon yet.

- [x] XDG paths: `teams.db`, transcripts dir, worktrees parent, `daemon.lock` under state root
- [x] SQLite WAL + `busy_timeout`; schema + migrations for `projects`, `epics`, `tasks`, `runs`, `events`, `approvals`, `messages`
- [x] Unique open-approval indexes (at most one unresolved approval per task and per epic)
- [x] `myai teams init`: create DB and register first project
- [x] Project config as data: roster, pipeline stages/roles/gates, concurrency ceilings, budgets, standups, notifications, `epic_checks`
- [x] Default concurrency when omitted: per-stage 1, `max_total` unset, `pm: 1`
- [x] `teams project new|edit|list` (YAML edit → stored `config_json`)
- [x] Default role prompt files referenced by roster (`prompts/pm.md`, designer, developer, qa, …)
- [x] Epic statuses: `grooming|awaiting_approval|executing|awaiting_review|done|abandoned`
- [x] Task statuses: `draft|backlog|ready|running|waiting_human|blocked|done|failed`; `blocked_by`, priority, stage, role, version, loop_count
- [x] `teams epic list|show|approve|abandon` (E-<id>)
- [x] `teams task add|edit|list|show` (T-<id>); `$EDITOR` markdown/YAML round-trip bumps `version`
- [x] Standalone tasks (no epic) supported alongside epic-linked tasks
- [x] `teams status [PROJECT]`: board overview, in-flight and queued-by-stage counts
- [x] CLI usable with daemon down (view/edit/queue only; no execution)

### Phase 13b: Agentic teams — skeleton hardening

Open items from the phase 13 review. Land before phase 14 builds on the schema and config.

- [ ] `create_task`: reject an `epic_id` belonging to a different project
- [ ] `UNIQUE` on `projects.name` (migration); drop the check-then-insert race in `create_project`
- [ ] `validate_config`: pipeline `role` must exist in the roster, stage names unique, `on_fail` must name a real stage, `per_stage`/`pm`/`max_total` values must be ints
- [ ] Set `busy_timeout` before `journal_mode = WAL` (the WAL switch takes a brief exclusive lock)
- [ ] `approve_epic`: stop bumping `tasks.version` on draft→backlog — version drives stale detection, and a status change is not an edit
- [ ] `bundled_prompts_dir()`: use `importlib.resources` like migrations, not `Path(__file__)`
- [ ] Decide: roster prompt paths relative (per design §4.1) vs absolute — absolute pins `config_json` to one `MYAI_HOME`
- [ ] Decide: nest teams state under `state_root()/teams/` (matching `sandbox/`) vs today's flat `prompts/`, `worktrees/`, `transcripts/`, `daemon.lock`
- [ ] Decide: task edit document should honor `epic_id`/`version`/`loop_count` or stop emitting them — currently shown and silently ignored
- [ ] Tests: capture CLI stdout; cover duplicate project names and `task add|show|list`

### Phase 14: Agentic teams — concurrent Cursor runs

Daemon orchestrates parallel isolated Cursor CLI agents on a develop-only pipeline.

- [ ] `teams daemon start|stop|status`: detached process; single instance via advisory `flock` on `daemon.lock`
- [ ] Daemon epoch (uuid) in lockfile; stamped on every launched `runs` row
- [ ] SQLite-as-IPC: CLI writes rows; daemon polls (~1–2s); no socket protocol
- [ ] Exactly-once transitions: message/`processed_at`, approval/`applied_at`, run outcome + task transition each in one txn
- [ ] Agents never write task state; DB lives outside agent workspaces
- [ ] Orchestrator owns prompt composition; adapters receive fully composed prompts only
- [ ] `AgentBackend` protocol + `RunResult` (outcome/summary/details/`needs_human`/transcript)
- [ ] Structured agent output: fenced JSON result block + `<needs_human>` sentinel; unparseable → `error` + inbox
- [ ] Cursor CLI adapter spike: resume flags, non-interactive/yolo flags, `--workspace` vs CWD, `--model`, multi-agent worktree conflict
- [ ] Cursor CLI adapter: resolve `agent`/`cursor-agent`; `agent -p --output-format json`; wall-clock timeout; tee transcript
- [ ] Spawn each run in its own process group (`setsid`); record `pid`/`pgid`/`proc_start` on `runs`
- [ ] `daemon stop` group-kills every live run; worktrees left in place
- [ ] Scheduler: count in-flight by stage; dispatch only real ready work; never invent tasks to fill slots
- [ ] Honor `concurrency.per_stage` and optional `max_total` as ceilings; claim order: DB `runs`+`running` txn, then worktree, then spawn
- [ ] Candidate order: priority desc, then older ready first; skip if open approval or over `max_runs_per_task`
- [ ] Task-lifetime sticky branch + worktree (`teams/T-<id>` for standalone); path under `worktrees/<project>/T-<id>/`
- [ ] Cursor always bound to task worktree, never primary `workspace_path`; require git repo for parallel agents
- [ ] One-stage `develop` pipeline end-to-end with multiple simultaneous children without shared-tree conflicts
- [ ] Session `resume_token` support (optimization; cold-start if unsupported)
- [ ] `runs` / `events` recording; `teams log T-<id>` (events + transcripts)
- [ ] Restart reconciliation: stale-epoch in-flight runs → parse transcript or mark `error`; re-queue or inbox
- [ ] Orphan cleanup: match `(pid, proc_start)` before kill; confirm with user (`[y/N]`)
- [ ] Sweep unmanaged worktree dirs not referenced by any live task after reconciliation

### Phase 15: Agentic teams — pipeline + epic integration

Full per-task stage machines, gates/inbox, and one finished local epic branch.

- [ ] User-defined multi-stage pipeline as project data (bespoke stages without source changes)
- [ ] Independent per-task FSM: siblings advance on their own clocks under shared stage ceilings
- [ ] Gate policies: `auto`, `human` (always pause), `epic` (pause standalone only)
- [ ] Agent-initiated `needs_human` and bound exhaustion (`max_loops`, `max_runs_per_task`) open the same inbox shape
- [ ] Gated task enters `waiting_human` and frees its stage slot immediately
- [ ] QA `on_fail` bounce to prior stage; `loop_count` increments; escalate at `max_loops`; reset past qa
- [ ] Mid-flight edit detection: `runs.task_version` vs current → `stale` + inbox, no auto-transition
- [ ] `teams inbox`: pending approvals + escalations (non-blocking; no terminal `[y/n]`)
- [ ] `teams approve|reject` with `-m` note routed back as feedback; apply only resolved-unapplied rows
- [ ] Epic branch cut at scope approval: `teams/E-<id>` from `base_branch`
- [ ] Task branches `teams/E-<n>/T-<m>`; sticky across stages on one task-lifetime worktree
- [ ] All daemon git integration in a daemon-owned epic worktree; never merge in the human's primary checkout
- [ ] Rebase task onto epic tip before final verifying stage (qa); rebase conflict counts as QA fail bounce
- [ ] Promote: merge task → epic (serialized, idempotent); promote conflict → bounce to develop
- [ ] Failed/stale/error runs never promote; worktree retained for retry
- [ ] Optional `epic_checks` command on epic branch (default: at epic completion only)
- [ ] When all tasks `done`: park epic `awaiting_review` with finished local branch; never push or open PRs
- [ ] `teams epic approve` marks epic `done` and prunes task branches; review notes short of accept → new tasks on same branch
- [ ] Deliverables live in task worktree then promote with the branch; downstream stages read them as repo files
- [ ] Worktree remove on task `done`/abandon; task branches persist until epic merge/prune policy

### Phase 16: Agentic teams — PM layer

Human talks to the PM; agents execute. Grooming, chat, plan revisions, standups.

- [ ] Daemon poll: unprocessed `messages` → PM; resolved approvals → apply transitions; then dispatch; then standups
- [ ] Stateless PM: every invocation cold from project config, board, events, open approvals, message thread
- [ ] Dedicated `concurrency.pm` budget so PM work does not starve task workers (default 1)
- [ ] Read-only PM asks may use primary `workspace_path`; mutating PM paths take a worktree
- [ ] `teams ask`: one-shot read-only → `from_pm` reply; no task mutation
- [ ] `teams tell`: new goal → epic grooming; correction → gated `plan_revision` (v1: explicit flags/phrasing)
- [ ] `teams chat [PROJECT]`: interactive PM session; approve drafted revisions/scope inline
- [ ] Epic intake: create epic in `grooming`; dedicated `groom_prompt` planning persona
- [ ] Conversational groom loop: epic-threaded `messages`, live `draft` tasks, human `$EDITOR` mid-groom
- [ ] PM may explore repo between turns ("look into that and get back"); transcript + findings resume across sittings
- [ ] Question batches as `approvals` kind `question`; answers via inbox or chat
- [ ] Finalize via PM judgment or human "go with what you have" (state remaining assumptions)
- [ ] `epic_approval`: refined goal, task list + deps, assumptions; approve → drafts → backlog/ready, cut epic branch
- [ ] Reject scope → back to grooming with note
- [ ] Ungated in-epic grooming after approval (split/add/reorder/edit); goal/scope changes need fresh `epic_approval`
- [ ] Plan revision: structured task-patch in `payload_json` + free-form summary; daemon applies patch only after human approve
- [ ] Rejection of plan/stage gates routes feedback to PM as re-plan trigger
- [ ] PM-composed epic handoff summary (PR-description-ready) at `awaiting_review`
- [ ] Rolling grooming-notes summary on epic to bound prompt context (drop old turns)
- [ ] `teams standup [--now | --schedule]`: PM digest of events since last standup
- [ ] Standup delivery v1: terminal nudge on next CLI use + digest file
- [ ] Notification adapter interface (inbox + standups) so later channels plug in without scheduler changes

### Phase 17: Agentic teams — polish & second backend

- [ ] Claude Code CLI adapter (`claude -p`, stream-json, `--resume`) behind the same contract
- [ ] Local llamacpp agent harness milestone (tool-calling loop) — stub/contract only until ready
- [ ] Webhook notification adapter
- [ ] Matrix notification adapter
- [ ] TUI exploration (Textual) over the same DB
- [ ] Export/import groundwork for external task systems (Jira, etc.)
- [ ] Optional `claimed_at` lease to avoid re-running interrupted PM/backend work after crash
- [ ] Auto-detect free-form tell as goal vs correction (today: explicit flags)
- [ ] Detect PM tasks outside approved epic goal (today: epic branch review is the backstop)
- [ ] Launchd / systemd user unit for `teams daemon`
- [ ] Model routing semantics per role via roster `model_hint` (cheap QA vs strong design)
