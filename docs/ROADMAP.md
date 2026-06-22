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
- [x] Renderers: cursor `.mdc`, claude/pi managed blocks in `CLAUDE.md`/`AGENTS.md`, skill dirs
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
