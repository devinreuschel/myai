import test from 'node:test'
import assert from 'node:assert/strict'
import { createShadowPathPredicate } from '@earendil-works/gondolin'

test('createShadowPathPredicate hides .myai subtree', () => {
  const shouldShadow = createShadowPathPredicate(['/.myai'])
  assert.equal(shouldShadow('/.myai'), true)
  assert.equal(shouldShadow('/.myai/sandbox.json'), true)
  assert.equal(shouldShadow('/src/main.py'), false)
})
