# myai

A local LLM runner built on llama.cpp. It manages the llama.cpp build, your models, and the server so you can pull a model and talk to it from the terminal, the way you'd use ollama.

The difference: instead of shipping a vendored binary, myai clones and builds llama.cpp itself. You pick the version (latest release, a pinned tag, or upstream HEAD), myai builds it, caches it, and keeps the `llama-cli` / `llama-server` tools on your path.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the project roadmap.

## Agent rule sync

Central repo for rules, skills, and subagents synced to managed repos (cursor, claude, pi).

```bash
myai master init              # scaffold master repo dirs
myai init --agent cursor --rule general   # per-repo config
myai init --rule langs,general --skill all   # CSV lists; 'all' = entire catalog
myai init --flat-rules        # flatten rules into AGENTS.md/CLAUDE.md
myai sync                     # apply rules/skills to agent-native paths
myai status                   # drift summary
myai global init --agent claude --skill demo   # user-home selection
myai global sync              # apply to ~/.claude, ~/.cursor, ~/.pi/agent
myai global status
```

Config lives in `.myai/config.json`. Key fields:

| Field | Default | Purpose |
|-------|---------|---------|
| `agents` | all three | which tools to sync (`cursor`, `claude`, `pi`) |
| `rules` | `[]` | rule selectors from master repo (`all` = every top-level rule/dir) |
| `nested_rules` | `true` | nested rule files vs flattened blocks |

`--agent` / `--rule` / `--skill` / `--subagent` accept repeated flags or comma-separated values (`--rule a,b`). Dir selectors like `langs` still expand to that directory's rules. Empty lists sync nothing; `all` syncs the full master catalog for that kind (resolved on each sync).

With `nested_rules: true` (default), cursor writes `.cursor/rules/*.mdc` and claude writes `.claude/rules/*.md`; pi always flattens to `AGENTS.md`. With `nested_rules: false`, cursor and claude flatten too (cursor+pi share `AGENTS.md`). See [docs/DESIGN.md](docs/DESIGN.md) for details.

User-global sync (`myai global`) is a separate plane: selection in `~/.myai/global.json`, writes into agent homes so skills/rules apply across projects. Cursor global rules are not file-backed; only skills sync for cursor. Sync prompts before overwriting untracked files already in those homes (`-y` to confirm non-interactively).

## Requirements

On your machine:

- git
- Python 3.14+
- a C/C++ toolchain + cmake (to build llama.cpp). On macOS that's the Xcode command line
  tools (`xcode-select --install`).

For development:

- [uv](https://docs.astral.sh/uv/)

We keep the dependency list as short as we can. Prefer the standard library; pull in a
package only when stdlib makes us write something genuinely awful.

## Install

One-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/devinreuschel/myai/main/install.sh | sh
```

The script will:

1. Check for git and Python 3.14+
2. Offer to install anything missing (with your confirmation), using platform-specific tools:
   - **macOS:** Homebrew (`brew install git python@3.14`) or Xcode CLI tools for git
   - **Debian/Ubuntu:** `apt` + deadsnakes PPA for Python 3.14
   - **Fedora:** `dnf`
3. Show a summary of all changes and ask before proceeding
4. Clone myai into `~/.local/share/myai-app`, create an isolated venv, and symlink `myai` to `~/.local/bin`

**What gets changed on disk:**

| Path | Purpose |
|------|---------|
| `~/.local/share/myai-app` | git clone + Python venv |
| `~/.local/bin/myai` | symlink to the CLI |

The script does **not** modify shell config files, system Python, or global pip packages. If `~/.local/bin` is not on your PATH, it prints instructions to add it.

The clone lives apart from myai's own data (`~/.local/share/myai`, `~/.myai`), so `--uninstall` removes the app and leaves your state alone. It asks first, and refuses to delete a directory that is not a myai clone.

**Options:**

```bash
curl -fsSL ... | sh -s -- --yes              # non-interactive (CI)
curl -fsSL ... | sh -s -- --no-install-deps  # fail if git/Python missing
curl -fsSL ... | sh -s -- --uninstall        # remove install
```

**Environment overrides:** `MYAI_REPO`, `MYAI_REF`, `MYAI_INSTALL_DIR`, `MYAI_BIN_DIR`, `MYAI_ASSUME_YES`, `MYAI_NO_INSTALL_DEPS`

## Quick start (development)

```bash
uv sync
uv run myai --help
```

## Development

```bash
uv sync
uv run myai --help
uv run python -m myai --help
uv run python -m unittest discover -s tests
```

## Agentic teams

Long-lived bots that keep an identity, remember their work, and message each other. So far this is the state layer only: the SQLite store, bot homes, and task threads under XDG state (`$MYAI_HOME` or `~/.local/share/myai`). Nothing runs until the daemon lands. Design: [docs/agentic-teams-design.md](docs/agentic-teams-design.md).

```bash
myai teams init                                  # create the DB and register you
myai teams bot new "Dev Lead" --job "drives development slices"
myai teams bot edit                              # bot.yaml in $EDITOR
myai teams task add --title "…" [--body "…"]     # opens a thread in your DM with the bot
myai teams task edit T-1                         # status, owner, handoff note
myai teams status
```

Each bot gets a home at `teams/bots/<id>/` holding `bot.yaml`, `persona.md`, `constraints.md`, `playbooks/`, and its `workspace/`.

## Sandbox (pi in a micro-VM)

Run `pi` inside a Gondolin micro-VM via a Node sidecar that drives the Gondolin SDK directly. The repo is mounted at its real host path by default, so it feels like plain `pi` but the process is hardware-isolated. Project `.myai/` is hidden from the guest by default.

```bash
myai sandbox doctor          # check Node, npm, QEMU, disk, sidecar, etc.
myai sandbox init            # write .myai/sandbox.json
myai sandbox trust           # review and approve a repo's sandbox.json
myai sandbox provision       # one-time pi install + sidecar npm install
myai sandbox run             # interactive pi in the VM (cold boot each run)
myai sandbox run -- -- -p "hello"   # pass args to pi
myai sandbox run -- --resume <id>   # resume a pi session inside the VM
```

Prerequisites: Node.js >= 23.6, npm, QEMU (or krun on Apple Silicon), ~5 GiB free disk. First run downloads ~200MB of guest assets and npm-installs the Gondolin SDK into `$MYAI_HOME/sandbox/sidecar/`.

**Provisioning vs runtime:** pi and its tools (fd, ripgrep) install in a separate
one-time provisioning VM that allows npm/github. The host caches them under
`$MYAI_HOME/sandbox/` (`pi-prefix`, `pi-bin`, etc.). Interactive `sandbox run`
honors only your resolved network policy (`providers` + `allow_hosts` + loopback
hosts) — github/npm are not auto-allowed at runtime. `sandbox run` triggers
provisioning automatically when needed; use `--skip-provision` to skip or
`--reprovision` to force a refresh.

Config lives in repo `.myai/sandbox.json`, layered over global
`~/.myai/sandbox.json`; the repo file overrides only the keys it names. Cloud API
access uses `providers`/`allow_hosts` and `host_secrets`.

### Trusting a repo's config

`.myai/sandbox.json` comes with the clone, yet it decides what the sandboxed
agent can reach: which hosts, which of your env secrets go where, which host
ports, your ssh agent. So myai ignores it until you approve it:

```bash
myai sandbox trust                 # shows what the config grants, then asks
myai sandbox trust --revoke
myai sandbox run --ignore-repo-config   # run on your global config alone
```

Approval pins the file's hash, so any edit (yours, a `git pull`, anything else)
needs a fresh look. `myai sandbox init` approves the defaults it writes.

Three keys pick code that runs on the host or in the pi cache every repo shares,
so they are read from the global config only and a repo's values are ignored
with a warning: `gondolin_package`, `gondolin_version`, `pi_package`. They take
a plain npm name and an exact version; URLs, ranges, and git refs are rejected.
The sidecar's SDK installs with `npm ci --ignore-scripts` from a shipped
lockfile.

### Network policy

The sandbox is **fail-closed**: by default (`network_policy: "custom"`) the guest
can reach only the hosts you resolve, and an empty allow list blocks everything
rather than opening the network. Pick providers by name instead of typing
hostnames:

```json
{
  "version": 2,
  "providers": ["anthropic"],
  "host_secrets": [{ "name": "ANTHROPIC_API_KEY", "hosts": ["api.anthropic.com"] }]
}
```

Known providers: `anthropic`, `openai`, `openrouter`, `gemini`, `github-copilot`,
`github`, `ollama`, `llama.cpp`. Combine `--provider` (repeatable), `--allow-host`,
and `--network {custom,deny-all,allow-all}` for one-off runs. `allow-all` disables
egress filtering entirely (prints a warning) — use it only when you trust the
task. `deny-all` means no network at all, host loopback routes included; for
"only my local model" use `custom` with an empty allow list plus loopback.

Host patterns are a hostname or `*.example.com`. Gondolin's `*` matches any
substring, dots included, so `*`, `*.com`, and `*github.com` (which matches
`evilgithub.com`) are rejected; use `allow-all` if you mean everything.

`host_secrets` values are read from the host env (or a renamed var via
`env_var`) and injected without ever touching the command line; their hosts must
also be allowed. The guest only ever holds a placeholder, under every policy;
the real value is swapped in for requests to that secret's hosts.

An allowed host is reachable for any purpose. Multi-tenant ones (`api.github.com`,
`www.googleapis.com`, an LLM API) can carry data out to an account that is not
yours, so allow what the task needs and no more.

### Host loopback

Off by default (cloud-first). Set `host_loopback.enabled` to `true` to map host
ports into the guest so it can reach local services (models, MCP, etc.):

```json
{
  "version": 2,
  "host_loopback": {
    "enabled": true,
    "routes": [
      {
        "id": "model",
        "guest_host": "model.host",
        "upstream": "http://localhost:8080/v1",
        "provision": { "provider": "myai-local", "model_id": "local" }
      }
    ]
  }
}
```

Upstreams must be this machine. To bridge the guest to another host (a model
server on your LAN), set `host_loopback.allow_remote_upstreams` or pass
`--allow-remote-upstream`. Link-local addresses (cloud metadata) are never
bridged.

`MYAI_MODEL_ENDPOINT` or `--model-endpoint URL` enables loopback for a single
run. Override with `--host-loopback` / `--no-host-loopback` or
`MYAI_HOST_LOOPBACK=1|0`. With `routes` empty, the legacy flat fields
(`model_endpoint`, `guest_model_host`, `provider`, `model_id`) are used instead.

### Rule injection 

For myai-managed repos that do not target pi (see `.myai/config.json`), the sandbox flattens the repo's selected rules into `/root/.pi/agent/AGENTS.md` so guest `pi` still gets project rules. Unmanaged repos and repos with `pi` in `agents` get no injected file — pi-managed repos use the synced repo `AGENTS.md` from the workspace mount.

### Other knobs

- `mirror_host_pi` / `--mirror-host-pi`: mirror host `~/.pi/agent/settings.json` (packages, default provider/model/thinking level, theme) into the guest. Localhost provider URLs are rewritten to the loopback host. Packages install once during provisioning (git/npm/github allowed there, not at runtime).
- `llama_server_url`: passed to the guest as `LLAMA_SERVER_URL` (localhost rewritten to the loopback host) for the `pi-llama-cpp` extension.
- `share_host_sessions` (default true): share this repo's slot of host `~/.pi/agent/sessions` with the guest, so `pi --resume <id>` works on the host or via `myai sandbox run -- --resume <id>`. Other projects' transcripts are not mounted, and symlinks the guest leaves in the slot are removed after the run.
- `guest_hidden_paths` / `--hide`: extra workspace paths hidden and denied in the guest. `/.myai` is always hidden and `--hide` adds to the list. Matching ignores case and follows symlinks.
- `git_access` (default `read-only`) / `--git-commit` / `--git-write`: how the guest may use git (see [Git in the sandbox](#git-in-the-sandbox)).
- `guest_repo_mount`: `"host_path"` (default) mounts at the real absolute path so cross-resume lines up; `"workspace"` mounts at `/workspace` (no path leak). In workspace mode the repo's real session slot is mounted as `--workspace--` so shared sessions still line up.
- `auto_approve` (default true) / `--no-auto-approve`: pi auto-approves tool calls inside the sandbox. Disable to require approval even when isolated.
- `rootfs_size` / `--rootfs-size`: grow the guest root disk (needs `e2fsprogs`).
- `--debug`: after the run, list executables the guest tried to run but couldn't find (handy when the agent reaches for a tool not installed in the image).

Each run cold-boots a fresh VM that exits when pi exits. Pi install and sidecar
deps are cached on the host under `$MYAI_HOME/sandbox/`; runs mount the pi caches
read-only, and only the provisioning VM can write them.

### Git in the sandbox

The base image has no git. Provisioning installs one and stages it in the
persistent cache (the binary in `pi-bin`, its helpers and libraries in
`git-bundle/`), so every run has a working git with no runtime network. Local
git — `status`, `diff`, `log`, `add`, `commit`, `branch`, `rebase` — all work;
remote transport (`fetch`/`push`) is not wired up.

`git_access` decides what the agent may do with it:

- **`read-only`** (default): the guest reads `.git` but cannot write it, so the
  agent inspects history and diffs but cannot commit. Writing `.git/hooks` or
  `.git/config` would run on the host the next time you use git, so the whole
  `.git` — every one in the workspace — is write-blocked; the agent cannot
  `git init` or clone into the tree either (`/tmp` works for that).
- **`commit`** / `--git-commit`: the agent commits normally, but into a scratch
  clone that borrows the repo's objects read-only. The real `.git` stays
  read-only. When the run ends, myai imports the branches the agent produced into
  `refs/sandbox/<run>/*` on your repo — nothing touches your branches. Review and
  merge like a pull request:

  ```bash
  git log --oneline refs/sandbox/          # what the agent produced
  git diff main refs/sandbox/<run>/<branch>
  git merge refs/sandbox/<run>/<branch>    # if you want it
  ```

  The agent's working-tree edits are on disk as usual (the tree is mounted
  read-write); the commits are what land under `refs/sandbox`.
- **`write`** / `--git-write`: the guest writes the real `.git` directly. Hooks
  and config it leaves there run on the host — an escape hatch, not a default.

### What the sandbox does not cover

The workspace is mounted read-write, and what the agent writes there you will
later open on the host. It cannot touch `.git` (unless `git_access: write`) or
`.myai`, but build scripts, `package.json` hooks, `.envrc`, editor task files,
and agent rule files are all ordinary files. Review a sandboxed agent's diff the
way you would a stranger's pull request. Pass `--ro` when the task only needs to
read.

`sandbox run` refuses a directory that contains your home, `$MYAI_HOME`, or
`~/.myai`.

Custom pi image: see [myai/sandbox/image/README.md](myai/sandbox/image/README.md).
