# myai

Local LLM management for people who want Ollama's ergonomics without Ollama's baggage.

myai handles llama.cpp lifecycle, model storage, and server orchestration so you can pull a model, load it, and talk to it — without babysitting builds, binaries, or config files.

## Goals

### llama.cpp management

Automatic install and updates for llama.cpp:

- **Latest release** — track stable tagged releases
- **Pinned version** — lock to a specific release the user chooses
- **Bleeding edge** — build from the latest upstream commit

Build, cache, and swap binaries without manual intervention.

### Server lifecycle

Run and manage the llama.cpp server: start, stop, restart, health checks, and sensible defaults for host, port, and runtime flags.

### Model management

Full model workflow, Ollama-style:

- Download and cache models
- List, inspect, and remove local models
- Expose models for use by clients and tools
- Load and unload models into the running server (jukebox semantics — swap what's loaded without restarting the whole stack)

### API compatibility

100% passthrough parity with the Ollama HTTP API so existing clients, scripts, and integrations work unchanged.

## Status

Early scaffolding. Nothing here yet except intent.

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/) for dependency and project management

## Quick start

```bash
uv sync
uv run myai --help
```

## Development

```bash
uv sync
uv run python main.py
```
