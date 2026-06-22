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
uv run python -m unittest discover -s tests
```

## Sandbox (pi in a micro-VM)

Run `pi` inside a Gondolin micro-VM. The repo is mounted at its real host path by
default, so it feels like plain `pi` but the process is hardware-isolated.

```bash
myai sandbox doctor          # check Node, QEMU, disk, etc.
myai sandbox init            # write .myai/sandbox.json
myai sandbox provision       # one-time pi install (allows npm/github)
myai sandbox run             # interactive pi in the VM
myai sandbox run -- -- -p "hello"   # pass args to pi
```

Prerequisites: Node.js >= 23.6, QEMU (or krun on Apple Silicon), ~5 GiB free
disk. First run downloads ~200MB of guest assets.

**Provisioning vs runtime:** pi and its tools (fd, ripgrep) install in a separate
one-time provisioning VM that allows npm/github. The host caches them under
`$MYAI_HOME/sandbox/` (`pi-prefix`, `pi-bin`, etc.). Interactive `sandbox run`
honors only your `allow_hosts` (+ loopback hosts) — github/npm are not
auto-allowed at runtime. `sandbox run` triggers provisioning automatically when
needed; use `--skip-provision` to skip or `--reprovision` to force a refresh.

Config lives in repo `.myai/sandbox.json`, which overrides global
`~/.myai/sandbox.json`. Cloud API access uses `allow_hosts` and `host_secrets`.

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

`MYAI_MODEL_ENDPOINT` or `--model-endpoint URL` enables loopback for a single
run. Override with `--host-loopback` / `--no-host-loopback` or
`MYAI_HOST_LOOPBACK=1|0`. With `routes` empty, the legacy flat fields
(`model_endpoint`, `guest_model_host`, `provider`, `model_id`) are used instead.

### Other knobs

- `mirror_host_pi` / `--mirror-host-pi`: mirror host `~/.pi/agent/settings.json`
  (packages, default provider/model/thinking level, theme) into the guest.
  Localhost provider URLs are rewritten to the loopback host. Packages install
  once during provisioning (git/npm/github allowed there, not at runtime).
- `llama_server_url`: passed to the guest as `LLAMA_SERVER_URL` (localhost
  rewritten to the loopback host) for the `pi-llama-cpp` extension.
- `share_host_sessions` (default true): bind host `~/.pi/agent/sessions` so host
  and guest `pi` share one pool. `pi -r` / `pi --session <id>` work in both.
- `guest_repo_mount`: `"host_path"` (default) mounts at the real absolute path so
  cross-resume lines up; `"workspace"` mounts at `/workspace` (no path leak, but
  cross-resume cwd may not line up).
- `rootfs_size` / `--rootfs-size`: grow the guest root disk (needs `e2fsprogs`).

Warm reuse: `myai sandbox register <session-id>`, `myai sandbox ls`,
`myai sandbox snapshot <id> --repo .`, `myai sandbox stop --repo .`.

Custom pi image: see [myai/sandbox/image/README.md](myai/sandbox/image/README.md).
