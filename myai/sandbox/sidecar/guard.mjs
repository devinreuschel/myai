/**
 * Path policy for the workspace mount, used as the ShadowProvider predicate.
 *
 * Two rules: hidden paths (`/.myai`) do not exist for the guest at all, and
 * anything under a `.git` stays readable but cannot be written. `.git/hooks`
 * and `.git/config` run on the host the next time git does, so a guest that
 * can write them has left the VM.
 *
 * Gondolin's stock path predicate is not enough on its own for either rule:
 * it compares names case-sensitively, while APFS and NTFS treat `.MYAI` and
 * `.myai` as one directory; and its symlink check realpaths the target, which
 * fails for a file that does not exist yet, so `ln -s .git g; touch g/hooks/x`
 * sails through. Here names are case-folded, and writes are re-checked against
 * the nearest ancestor that does exist.
 *
 * No Gondolin imports, so this is testable without the SDK installed.
 */

import fs from 'node:fs'
import nodePath from 'node:path'

const WRITE_OPS = new Set(['mkdir', 'rmdir', 'unlink', 'rename', 'symlink', 'link'])
// These act on the directory entry itself, so a symlink there is not followed.
const ENTRY_OPS = new Set(['mkdir', 'rmdir', 'unlink', 'rename', 'symlink', 'link'])
const MAX_LINK_HOPS = 40

const O_WRONLY = 0o1
const O_RDWR = 0o2
const O_CREAT = 0o100
const O_TRUNC = 0o1000
const O_APPEND = 0o2000

/**
 * @param {string | number | undefined} flags
 * @returns {boolean}
 */
export function isWriteOpen (flags) {
  if (typeof flags === 'number') {
    return (flags & (O_WRONLY | O_RDWR | O_CREAT | O_TRUNC | O_APPEND)) !== 0
  }
  return /[wa+]/.test(String(flags ?? 'r'))
}

/**
 * @param {string} input
 * @returns {string} absolute posix path without a trailing slash
 */
export function normalizeVfsPath (input) {
  let p = nodePath.posix.normalize(String(input))
  if (!p.startsWith('/')) p = `/${p}`
  if (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1)
  return p
}

/**
 * Fold a path the way a case-insensitive host filesystem would when matching
 * names. Upper-then-lower catches the few characters that only fold one way
 * (long s, Kelvin sign). Over-folding just hides an odd name; under-folding
 * would let one through.
 * @param {string} p
 * @returns {string}
 */
function fold (p) {
  return p.normalize('NFKC').toUpperCase().toLowerCase().normalize('NFKC')
}

/**
 * @param {string} hostRoot
 * @param {{ hiddenPaths?: string[], gitReadonly?: boolean }} [options]
 * @returns {(info: { op: string, path: string, flags?: string | number }) => boolean}
 */
export function createWorkspaceGuard (hostRoot, options = {}) {
  const hidden = [...new Set((options.hiddenPaths ?? []).map(p => fold(normalizeVfsPath(p))))]
    .filter(p => p !== '/')
  const gitReadonly = options.gitReadonly ?? true

  let root = nodePath.resolve(hostRoot)
  try {
    root = fs.realpathSync.native(root)
  } catch {
    // keep the lexical root; resolution below then fails closed
  }

  const isHidden = folded => hidden.some(h => folded === h || folded.startsWith(`${h}/`))
  const inGit = folded => folded.split('/').includes('.git')

  /**
   * Where a guest path really lands, as a VFS path, or null if it leaves the
   * mount. Existing ancestors are realpath'd; the part that does not exist yet
   * is appended as written.
   * @param {string} vfsPath
   * @param {boolean} followLeaf
   */
  const resolve = (vfsPath, followLeaf) => {
    const parts = vfsPath.split('/').filter(Boolean)
    if (parts.length === 0) return '/'
    const leaf = followLeaf ? null : parts.pop()

    let current = root
    let hops = 0
    const pending = [...parts]
    while (pending.length > 0) {
      const next = nodePath.join(current, pending[0])
      let stat
      try {
        stat = fs.lstatSync(next)
      } catch {
        break // from here on nothing exists; the rest is taken literally
      }
      pending.shift()
      if (stat.isSymbolicLink()) {
        if (++hops > MAX_LINK_HOPS) return null
        const target = fs.readlinkSync(next)
        // Re-walk the target so a dangling link still resolves to where a
        // create through it would land.
        const base = nodePath.isAbsolute(target) ? '/' : current
        const joined = nodePath.resolve(base, target)
        if (joined !== root && !joined.startsWith(root + nodePath.sep)) return null
        pending.unshift(...nodePath.relative(root, joined).split(nodePath.sep).filter(Boolean))
        current = root
      } else {
        current = next
      }
    }

    let real = nodePath.join(current, ...pending)
    if (leaf !== null) real = nodePath.join(real, leaf)
    if (real === root) return '/'
    if (!real.startsWith(root + nodePath.sep)) return null
    return `/${nodePath.relative(root, real).split(nodePath.sep).join('/')}`
  }

  return ({ op, path, flags }) => {
    const p = normalizeVfsPath(path)
    const folded = fold(p)
    if (isHidden(folded)) return true

    const writing = WRITE_OPS.has(op) || (op === 'open' && isWriteOpen(flags))
    if (!writing) return false
    if (gitReadonly && inGit(folded)) return true

    // Only writes pay for touching the disk; reads through a link to something
    // that exists are caught by ShadowProvider's own realpath pass.
    const resolved = resolve(p, !ENTRY_OPS.has(op))
    if (resolved === null) return true
    const resolvedFolded = fold(resolved)
    return isHidden(resolvedFolded) || (gitReadonly && inGit(resolvedFolded))
  }
}
