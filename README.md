# myai

A local LLM runner built on llama.cpp. It manages the llama.cpp build, your models, and the server so you can pull a model and talk to it from the terminal, the way you'd use ollama.

The difference: instead of shipping a vendored binary, myai clones and builds llama.cpp itself. You pick the version (latest release, a pinned tag, or upstream HEAD), myai builds it, caches it, and keeps the `llama-cli` / `llama-server` tools on your path.

## Status

Scaffolding. The plan below is what we're building toward, in order.

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
4. Clone myai into `~/.local/share/myai`, create an isolated venv, and symlink `myai` to `~/.local/bin`

**What gets changed on disk:**

| Path | Purpose |
|------|---------|
| `~/.local/share/myai` | git clone + Python venv |
| `~/.local/bin/myai` | symlink to the CLI |

The script does **not** modify shell config files, system Python, or global pip packages. If `~/.local/bin` is not on your PATH, it prints instructions to add it.

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

## Roadmap

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

## Development

```bash
uv sync
uv run myai --help
uv run python -m myai --help
```
