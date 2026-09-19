import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createWorkspaceGuard, isWriteOpen, normalizeVfsPath } from './guard.mjs'

/**
 * A workspace on disk with a .myai, a .git, and some source.
 * @param {import('node:test').TestContext} t
 * @returns {string}
 */
function workspace (t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'myai-guard-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.mkdirSync(path.join(root, '.myai'))
  fs.writeFileSync(path.join(root, '.myai', 'sandbox.json'), '{}')
  fs.mkdirSync(path.join(root, '.git', 'hooks'), { recursive: true })
  fs.writeFileSync(path.join(root, '.git', 'config'), '[core]\n')
  fs.mkdirSync(path.join(root, 'src'))
  fs.writeFileSync(path.join(root, 'src', 'a.py'), 'x')
  return root
}

const read = p => ({ op: 'open', path: p, flags: 'r' })
const write = p => ({ op: 'open', path: p, flags: 'w' })

test('hidden paths are shadowed for every op, reads included', t => {
  const guard = createWorkspaceGuard(workspace(t), { hiddenPaths: ['/.myai'] })
  assert.equal(guard({ op: 'stat', path: '/.myai' }), true)
  assert.equal(guard(read('/.myai/sandbox.json')), true)
  assert.equal(guard({ op: 'readdir', path: '/.myai' }), true)
  assert.equal(guard(read('/src/a.py')), false)
  assert.equal(guard({ op: 'stat', path: '/.myai-notes' }), false)
})

test('hidden paths match whatever case the guest spells them in', t => {
  const guard = createWorkspaceGuard(workspace(t), { hiddenPaths: ['/.myai'] })
  assert.equal(guard(read('/.MYAI/sandbox.json')), true)
  assert.equal(guard({ op: 'mkdir', path: '/.Myai/x' }), true)
})

test('.git can be read but not written', t => {
  const guard = createWorkspaceGuard(workspace(t), { hiddenPaths: ['/.myai'] })
  assert.equal(guard(read('/.git/config')), false)
  assert.equal(guard({ op: 'readdir', path: '/.git' }), false)
  assert.equal(guard({ op: 'stat', path: '/.git/hooks' }), false)

  assert.equal(guard(write('/.git/config')), true)
  assert.equal(guard({ op: 'open', path: '/.git/config', flags: 'r+' }), true)
  assert.equal(guard({ op: 'open', path: '/.git/config', flags: 'a' }), true)
  assert.equal(guard(write('/.git/hooks/pre-commit')), true)
  assert.equal(guard(write('/.GIT/hooks/pre-commit')), true)
  assert.equal(guard({ op: 'unlink', path: '/.git/config' }), true)
  assert.equal(guard({ op: 'mkdir', path: '/.git/x' }), true)
})

test('.git itself cannot be moved aside, replaced, or created', t => {
  const guard = createWorkspaceGuard(workspace(t), {})
  assert.equal(guard({ op: 'rename', path: '/.git' }), true)
  assert.equal(guard({ op: 'rename', path: '/.git/moved-in' }), true)
  assert.equal(guard({ op: 'rmdir', path: '/.git' }), true)
  // a nested repo is one `cd` (or an IDE scan) away from running its hooks
  assert.equal(guard({ op: 'mkdir', path: '/src/.git' }), true)
  assert.equal(guard({ op: 'symlink', path: '/src/.git' }), true)
  assert.equal(guard(write('/src/.git')), true)
})

test('names that merely start with .git are ordinary files', t => {
  const guard = createWorkspaceGuard(workspace(t), {})
  assert.equal(guard(write('/.gitignore')), false)
  assert.equal(guard(write('/.github/workflows/ci.yml')), false)
  assert.equal(guard(write('/src/.gitkeep')), false)
})

test('a new file cannot be created in a protected dir through a symlink', t => {
  const root = workspace(t)
  const guard = createWorkspaceGuard(root, { hiddenPaths: ['/.myai'] })
  fs.symlinkSync('.git', path.join(root, 'g'))
  fs.symlinkSync('.myai', path.join(root, 'm'))
  fs.symlinkSync('.MYAI', path.join(root, 'm2'))

  // none of these targets exist yet, so realpath on the full path would fail
  assert.equal(guard(write('/g/hooks/pre-commit')), true)
  assert.equal(guard({ op: 'mkdir', path: '/g/hooks/dir' }), true)
  assert.equal(guard(write('/m/planted.json')), true)
  assert.equal(guard(write('/m2/planted.json')), true)
  assert.equal(guard({ op: 'rename', path: '/g/config' }), true)
})

test('a dangling symlink is resolved to where a create would land', t => {
  const root = workspace(t)
  const guard = createWorkspaceGuard(root, {})
  fs.symlinkSync('.git/hooks/post-checkout', path.join(root, 'dangling'))
  fs.symlinkSync('src/not-yet.py', path.join(root, 'harmless'))

  assert.equal(guard(write('/dangling')), true)
  assert.equal(guard(write('/harmless')), false)
  // removing the link itself touches nothing under .git
  assert.equal(guard({ op: 'unlink', path: '/dangling' }), false)
})

test('chained symlinks are followed', t => {
  const root = workspace(t)
  const guard = createWorkspaceGuard(root, {})
  fs.symlinkSync('.git', path.join(root, 'one'))
  fs.symlinkSync('one', path.join(root, 'two'))
  assert.equal(guard(write('/two/hooks/pre-push')), true)
})

test('a symlink loop or one leaving the mount fails closed for writes', t => {
  const root = workspace(t)
  const guard = createWorkspaceGuard(root, {})
  fs.symlinkSync('loop-b', path.join(root, 'loop-a'))
  fs.symlinkSync('loop-a', path.join(root, 'loop-b'))
  fs.symlinkSync(os.tmpdir(), path.join(root, 'out'))

  assert.equal(guard(write('/loop-a/x')), true)
  assert.equal(guard(write('/out/x')), true)
})

test('ordinary work is left alone', t => {
  const guard = createWorkspaceGuard(workspace(t), { hiddenPaths: ['/.myai'] })
  assert.equal(guard(write('/src/new.py')), false)
  assert.equal(guard(write('/brand/new/tree/file.txt')), false)
  assert.equal(guard({ op: 'mkdir', path: '/build' }), false)
  assert.equal(guard({ op: 'rename', path: '/src/a.py' }), false)
  assert.equal(guard({ op: 'unlink', path: '/src/a.py' }), false)
})

test('gitReadonly: false lifts the .git shield but never the hidden paths', t => {
  const root = workspace(t)
  const guard = createWorkspaceGuard(root, { hiddenPaths: ['/.myai'], gitReadonly: false })
  fs.symlinkSync('.myai', path.join(root, 'm'))

  assert.equal(guard(write('/.git/hooks/pre-commit')), false)
  assert.equal(guard(write('/.myai/sandbox.json')), true)
  assert.equal(guard(write('/m/planted.json')), true)
})

test('extra hidden paths work and the root is never hidden', t => {
  const guard = createWorkspaceGuard(workspace(t), { hiddenPaths: ['/.myai', 'secrets/', '/'] })
  assert.equal(guard(read('/secrets/key.pem')), true)
  assert.equal(guard(read('/SECRETS/key.pem')), true)
  assert.equal(guard({ op: 'readdir', path: '/' }), false)
})

test('isWriteOpen understands node flag strings and numeric flags', () => {
  for (const flags of ['w', 'w+', 'wx', 'a', 'a+', 'ax', 'r+']) {
    assert.equal(isWriteOpen(flags), true, flags)
  }
  assert.equal(isWriteOpen('r'), false)
  assert.equal(isWriteOpen(undefined), false)
  assert.equal(isWriteOpen(0), false)
  assert.equal(isWriteOpen(0o1), true)
  assert.equal(isWriteOpen(0o2), true)
  assert.equal(isWriteOpen(0o100), true)
})

test('normalizeVfsPath', () => {
  assert.equal(normalizeVfsPath('a/b/'), '/a/b')
  assert.equal(normalizeVfsPath('/a/../b'), '/b')
  assert.equal(normalizeVfsPath('/'), '/')
})
