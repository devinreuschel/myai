# Design notes

## Agent sandbox: phasing and escape hatches

We run interactive `pi` inside a microVM so it feels like plain `pi` but the
process is hardware-isolated (own kernel, mediated network, secrets off the
guest). Constraints: Python-drivable, local/self-hosted with no third-party
account, real microVM isolation, and a true bidirectional PTY (raw stdin,
stdout/stderr, resize). The PTY requirement is the decisive criterion and rules
out most exec-only sandbox SDKs.

### Phase 2 (if the CLI flag surface is limiting): Node sidecar

A specific missing Gondolin capability (custom VFS provider, dynamic per-request
network policy, programmatic ingress) that the CLI doesn't expose. Write a small
`.mjs` that imports `@earendil-works/gondolin`, encodes our policy via
`VM.create()`, and exposes a narrow stdio/JSON-RPC interface our Python CLI
calls. Keeps the tool in Python while unlocking the full SDK. Cost: we bundle and
maintain a Node helper + `node_modules` and a host<->sidecar protocol; still a
Node runtime dependency.

### Phase 3 (if the Node dependency itself is the problem): Python-native swap

Triggered when the friction is "we don't want Node in a Python product" rather
than a missing Gondolin feature. Drop Gondolin for a microVM runtime with a
first-class Python SDK. **Gate: validate the candidate's PTY can carry a full
interactive `pi` session (attach, type, resize) before committing.** If it
can't, stay on Phase 1/2.

Options, ranked by fit:

- **microsandbox (primary).** Closest Python-native analog to Gondolin:
  local-first libkrun microVMs (~sub-100ms boot), embeddable Python SDK via pyo3
  with runtime binaries shipped in the wheel (no daemon/server/account),
  network-layer secret injection + network/DNS/TLS policy, runs OCI images,
  Apache-2.0. Risks: PTY maturity must be verified (docs lean on
  `exec`/`exec_stream`), pre-1.0, Linux+KVM or macOS Apple Silicon only.
- **E2B self-hosted (proven-PTY fallback).** Firecracker microVMs, mature Python
  SDK, first-class proven PTY, Apache-2.0. Downside: cloud-first; self-hosting
  means standing up their orchestrator (heavier ops than `pip install`).
- **Arrakis** cloud-hypervisor microVMs, self-hosted, REST + Python
  SDK, snapshot/restore (useful for rewind/MCTS agent flows). Downside:
  run-a-REST-server model and a VNC-leaning interactive story, a poor fit for a
  transparent terminal TUI.

Excluded by constraints: Modal (gVisor, cloud-only), Daytona (container/cloud),
Docker `sbx` (requires Docker account/login). DIY (drive libkrun/Firecracker
directly) means rebuilding the guest agent + PTY channel; not worth it unless a
hard requirement blocks every option above.
