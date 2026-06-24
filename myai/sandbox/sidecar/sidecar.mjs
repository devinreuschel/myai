#!/usr/bin/env node
/**
 * Gondolin SDK sidecar: reads a VM spec JSON file, boots a cold microVM,
 * runs the guest command, and exits with the guest exit code.
 */

import { readFileSync } from 'node:fs'
import process from 'node:process'
import {
  VM,
  RealFSProvider,
  ReadonlyProvider,
  ShadowProvider,
  createShadowPathPredicate,
  createHttpHooks,
} from '@earendil-works/gondolin'

/**
 * Load and parse the VM spec JSON file path passed on argv.
 * @param {string} specPath
 * @returns {Record<string, unknown>}
 */
function loadSpec (specPath) {
  return JSON.parse(readFileSync(specPath, 'utf8'))
}

/**
 * @param {Record<string, unknown>} spec
 * @returns {import('@earendil-works/gondolin').VMOptions['sandbox']}
 */
function sandboxOptions (spec) {
  const opts = { console: 'none' }
  if (process.platform === 'linux' && process.arch === 'x64') {
    opts.machineType = 'q35'
  }
  return opts
}

/**
 * Build workspace VFS provider with optional hidden paths and RO wrapper.
 * @param {Record<string, unknown>} workspace
 * @returns {import('@earendil-works/gondolin').VFSProvider}
 */
function workspaceProvider (workspace) {
  let provider = new RealFSProvider(workspace.hostPath)
  const hidden = workspace.hiddenPaths ?? []
  if (hidden.length > 0) {
    provider = new ShadowProvider(provider, {
      shouldShadow: createShadowPathPredicate(hidden),
    })
  }
  if (workspace.readonly) {
    provider = new ReadonlyProvider(provider)
  }
  return provider
}

/**
 * @param {Record<string, unknown>} spec
 * @returns {Record<string, import('@earendil-works/gondolin').VFSProvider>}
 */
function buildVfsMounts (spec) {
  const vfs = spec.vfs ?? {}
  const workspace = vfs.workspace ?? {}
  const mounts = {
    [workspace.guestPath]: workspaceProvider(workspace),
  }
  for (const mount of vfs.mounts ?? []) {
    let provider = new RealFSProvider(mount.hostPath)
    if (mount.readonly) {
      provider = new ReadonlyProvider(provider)
    }
    mounts[mount.guestPath] = provider
  }
  return mounts
}

/**
 * Resolve secret values from the sidecar process env (set by Python host).
 * @param {Record<string, { hosts: string[] }>} declared
 * @returns {{ resolved: Record<string, { hosts: string[], value: string }>, missing: string[] }}
 */
function resolveSecrets (declared) {
  const resolved = {}
  const missing = []
  for (const [name, cfg] of Object.entries(declared ?? {})) {
    const value = process.env[name]
    if (value == null) {
      missing.push(name)
      continue
    }
    resolved[name] = { hosts: cfg.hosts, value }
  }
  return { resolved, missing }
}

/**
 * @param {Record<string, unknown>} network
 * @param {Record<string, { hosts: string[], value: string }>} secrets
 */
function buildHttpHooks (network, secrets) {
  const policy = network.policy ?? 'custom'
  const allowedHosts = network.allowedHosts ?? []
  const hasSecrets = Object.keys(secrets).length > 0
  const tcpKeys = Object.keys(network.tcpHosts ?? {})

  if (policy === 'allow-all') {
    return { httpHooks: undefined, guestEnv: Object.fromEntries(
      Object.entries(secrets).map(([k, v]) => [k, v.value])
    ) }
  }

  const baseOpts = {
    allowedHosts: policy === 'deny-all' ? [] : allowedHosts,
  }
  if (tcpKeys.length > 0) {
    baseOpts.allowedInternalHosts = tcpKeys.map(k => k.split(':')[0])
  }
  if (hasSecrets) {
    baseOpts.secrets = secrets
  }
  const { httpHooks, env } = createHttpHooks(baseOpts)
  return { httpHooks, guestEnv: env ?? {} }
}

/**
 * @param {Record<string, unknown>} spec
 */
function buildVmOptions (spec) {
  const network = spec.network ?? {}
  const { resolved: secrets, missing } = resolveSecrets(network.secrets ?? {})
  for (const name of missing) {
    process.stderr.write(`warning: secret ${JSON.stringify(name)} configured but host env unset; not injected\n`)
  }
  const { httpHooks, guestEnv } = buildHttpHooks(network, secrets)
  const tcpHosts = network.tcpHosts ?? {}
  const hasTcp = Object.keys(tcpHosts).length > 0

  /** @type {import('@earendil-works/gondolin').VMOptions} */
  const opts = {
    sandbox: sandboxOptions(spec),
    httpHooks,
    startTimeoutMs: 0,
    vfs: { mounts: buildVfsMounts(spec) },
  }
  if (spec.image) {
    opts.sandbox = { ...opts.sandbox, imagePath: spec.image }
  }
  if (spec.vmm) {
    opts.sandbox = { ...opts.sandbox, vmm: spec.vmm }
  }
  if (spec.rootfsSize) {
    opts.rootfs = { size: spec.rootfsSize }
  }
  if (hasTcp) {
    opts.dns = { mode: 'synthetic', syntheticHostMapping: 'per-host' }
    opts.tcp = { hosts: tcpHosts }
  }
  const sshAllow = network.sshAllowHosts ?? []
  if (sshAllow.length > 0 || network.useSshAgent) {
    opts.ssh = {
      allowedHosts: sshAllow,
      ...(network.useSshAgent && process.env.SSH_AUTH_SOCK
        ? { agent: process.env.SSH_AUTH_SOCK }
        : {}),
    }
  }
  return { opts, guestEnv }
}

/**
 * @param {import('@earendil-works/gondolin').VM} vm
 * @param {Record<string, unknown>} spec
 * @param {Record<string, string>} guestEnv
 */
async function runCommand (vm, spec, guestEnv) {
  const envMap = { ...(spec.env ?? {}), ...guestEnv }
  const envList = Object.entries(envMap).map(([k, v]) => `${k}=${v}`)
  const command = spec.command ?? ['sh', '-lc', 'true']
  const cwd = spec.cwd

  if (spec.interactive) {
    const proc = vm.shell({
      env: envList,
      cwd,
      command,
      attach: true,
    })
    const result = await proc
    return result.exitCode ?? 1
  }

  const proc = vm.exec(command, {
    cwd,
    env: envList,
    stdout: 'inherit',
    stderr: 'inherit',
  })
  const result = await proc
  return result.exitCode ?? 1
}

async function main () {
  const specPath = process.argv[2]
  if (!specPath) {
    process.stderr.write('usage: sidecar.mjs <spec.json>\n')
    process.exit(2)
  }
  const spec = loadSpec(specPath)
  const { opts, guestEnv } = buildVmOptions(spec)
  const vm = await VM.create(opts)
  try {
    const code = await runCommand(vm, spec, guestEnv)
    process.exit(code)
  } finally {
    await vm.close()
  }
}

main().catch(err => {
  process.stderr.write(`${err?.stack ?? err}\n`)
  process.exit(1)
})
