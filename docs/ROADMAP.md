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
