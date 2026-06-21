# myai

A local LLM runner built on llama.cpp. It manages the llama.cpp build, your models, and the server so you can pull a model and talk to it from the terminal, the way you'd use ollama.

The difference: instead of shipping a vendored binary, myai clones and builds llama.cpp itself. You pick the version (latest release, a pinned tag, or upstream HEAD), myai builds it, caches it, and keeps the `llama-cli` / `llama-server` tools on your path.

## Status

Scaffolding. The plan below is what we're building toward, in order.

## Requirements

On your machine:

- git
- a C/C++ toolchain + cmake (to build llama.cpp). On macOS that's the Xcode command line
  tools (`xcode-select --install`).

For development:

- Python 3.14
- [uv](https://docs.astral.sh/uv/)

We keep the dependency list as short as we can. Prefer the standard library; pull in a
package only when stdlib makes us write something genuinely awful.

## Quick start

```bash
uv sync
uv run myai --help
```

## Roadmap

### Phase 1: CLI foundation

Turn the project into a CLI you run as `myai <command>`.

- [ ] Restructure from a single `main.py` into a `myai/` package
- [ ] Wire up the `myai` entry point so it runs as a command
- [ ] Command dispatch with per-subcommand modules

### Phase 2: Prerequisite checks

Verify the host has what we need before we try to build anything, with clear messages when something's missing.

- [ ] Detect git
- [ ] Detect cmake and a working compiler

### Phase 3: llama.cpp build lifecycle

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
uv run python main.py   # becomes `python -m myai` after roadmap step 1
```
