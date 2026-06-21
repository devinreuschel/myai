# myai

A local LLM runner built on llama.cpp. It manages the llama.cpp build, your models, and the server so you can pull a model and talk to it from the terminal, the way you'd use ollama.

The difference: instead of shipping a vendored binary, myai clones and builds llama.cpp itself. You pick the version (latest release, a pinned tag, or upstream HEAD), myai builds it, caches it, and keeps the `llama-cli` / `llama-server` tools on your path.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the project roadmap.

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

## Development

```bash
uv sync
uv run myai --help
uv run python -m myai --help
```
