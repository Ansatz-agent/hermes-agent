import assert from 'node:assert/strict'

import { describe, test } from 'vitest'

import {
  createBootstrapState,
  reduceBootstrapState,
  safeBootstrapEvent,
  safeBootstrapState
} from './bootstrap-progress'

const manifest = {
  type: 'manifest' as const,
  protocolVersion: 1,
  bootstrapScope: 'runtime' as const,
  stages: [
    {
      name: 'python-deps',
      title: 'Install Hermes Python dependencies',
      category: 'runtime',
      needs_user_input: false
    },
    { name: 'complete', title: 'Finish install', category: 'runtime', needs_user_input: false }
  ]
}

describe('bootstrap progress state', () => {
  test('records manifest, stage timing, and the latest known-total progress snapshot', () => {
    let state = reduceBootstrapState(createBootstrapState(), manifest, 1_000)

    assert.equal(state.active, true)
    assert.equal(state.startedAt, 1_000)
    assert.deepEqual(state.stages['python-deps'], {
      state: 'pending',
      durationMs: null,
      startedAt: null,
      json: null,
      error: null,
      progress: null
    })

    state = reduceBootstrapState(state, { type: 'stage', name: 'python-deps', state: 'running' }, 1_250)
    state = reduceBootstrapState(state, { type: 'stage', name: 'python-deps', state: 'running' }, 1_500)
    state = reduceBootstrapState(
      state,
      {
        type: 'progress',
        stage: 'python-deps',
        completed: 38_200_000,
        total: 126_500_000,
        unit: 'bytes',
        label: 'Hermes Python dependencies'
      },
      1_750
    )

    assert.equal(state.stages['python-deps']?.startedAt, 1_250)
    assert.deepEqual(state.stages['python-deps']?.progress, {
      stage: 'python-deps',
      completed: 38_200_000,
      total: 126_500_000,
      unit: 'bytes',
      label: 'Hermes Python dependencies',
      updatedAt: 1_750
    })

    state = reduceBootstrapState(
      state,
      {
        type: 'stage',
        name: 'python-deps',
        state: 'succeeded',
        durationMs: 900,
        json: { ok: true, stage: 'python-deps' }
      },
      2_150
    )

    assert.equal(state.stages['python-deps']?.durationMs, 900)
    assert.equal(state.stages['python-deps']?.progress?.completed, 38_200_000)
  })

  test('keeps unknown totals indeterminate and ignores invalid progress values', () => {
    let state = reduceBootstrapState(createBootstrapState(), manifest, 1_000)
    state = reduceBootstrapState(state, { type: 'stage', name: 'python-deps', state: 'running' }, 1_100)
    state = reduceBootstrapState(
      state,
      {
        type: 'progress',
        stage: 'python-deps',
        completed: 47,
        total: null,
        unit: 'packages',
        label: 'Installing dependencies'
      },
      1_200
    )

    assert.deepEqual(state.stages['python-deps']?.progress, {
      stage: 'python-deps',
      completed: 47,
      total: null,
      unit: 'packages',
      label: 'Installing dependencies',
      updatedAt: 1_200
    })

    const invalid = reduceBootstrapState(
      state,
      {
        type: 'progress',
        stage: 'python-deps',
        completed: Number.NaN,
        total: -1,
        unit: 'percent',
        label: 'fake'
      },
      1_300
    )

    assert.strictEqual(invalid, state)
  })

  test('ignores progress for a stage outside the signed manifest', () => {
    const state = reduceBootstrapState(createBootstrapState(), manifest, 1_000)
    const next = reduceBootstrapState(
      state,
      {
        type: 'progress',
        stage: 'not-in-manifest',
        completed: 1,
        total: 2,
        unit: 'items',
        label: 'Injected stage'
      },
      1_100
    )

    assert.strictEqual(next, state)
    assert.equal(next.stages['not-in-manifest'], undefined)
  })

  test('projects a log-free safe snapshot and redacts hostile labels and failures', () => {
    let state = reduceBootstrapState(createBootstrapState(), manifest, 1_000)
    state = reduceBootstrapState(state, { type: 'stage', name: 'python-deps', state: 'running' }, 1_100)
    state = reduceBootstrapState(
      state,
      {
        type: 'progress',
        stage: 'python-deps',
        completed: 1,
        total: null,
        unit: 'packages',
        label: 'password=secret Cookie: abc /Users/alice/private https://evil.invalid'
      },
      1_200
    )
    state = reduceBootstrapState(
      state,
      {
        type: 'log',
        stage: 'python-deps',
        stream: 'stderr',
        line: 'sessionid=secret csrf=hidden Keychain=/Users/alice/Library'
      },
      1_300
    )
    state = reduceBootstrapState(
      state,
      { type: 'failed', stage: 'python-deps', error: 'Bearer secret Traceback /Users/alice/private' },
      1_400
    )

    const safe = safeBootstrapState(state)
    const serialized = JSON.stringify(safe)

    assert.equal('log' in safe, false)
    assert.equal(safe.error, 'bootstrap_failed')
    assert.equal(safe.failedStage, 'python-deps')
    assert.equal(safe.stages['python-deps']?.progress?.label, 'Install Hermes Python dependencies')

    for (const forbidden of [
      'password',
      'secret',
      'Cookie',
      'sessionid',
      'csrf',
      'Keychain',
      '/Users/',
      'https://',
      'Traceback',
      'Bearer'
    ]) {
      assert.equal(serialized.includes(forbidden), false, forbidden)
    }
  })

  test('sanitizes live events and never forwards logs, result JSON, or raw errors', () => {
    let state = reduceBootstrapState(createBootstrapState(), manifest, 1_000)
    state = reduceBootstrapState(state, { type: 'stage', name: 'python-deps', state: 'running' }, 1_100)

    assert.equal(
      safeBootstrapEvent(
        { type: 'log', stage: 'python-deps', line: 'password=secret', stream: 'stderr' },
        state,
        1_200
      ),
      null
    )

    assert.deepEqual(
      safeBootstrapEvent(
        {
          type: 'stage',
          name: 'python-deps',
          state: 'failed',
          durationMs: 500,
          json: { ok: false, stage: 'python-deps', reason: 'session=secret' },
          error: 'password=secret'
        },
        state,
        1_300
      ),
      { type: 'stage', name: 'python-deps', state: 'failed', durationMs: 500, error: 'bootstrap_failed' }
    )

    assert.deepEqual(
      safeBootstrapEvent(
        { type: 'failed', stage: 'python-deps', error: 'Cookie: secret /Users/alice/private' },
        state,
        1_400
      ),
      { type: 'failed', stage: 'python-deps', error: 'bootstrap_failed' }
    )
  })

  test('restores the safe stage progress from the main-process snapshot', () => {
    let state = reduceBootstrapState(createBootstrapState(), manifest, 1_000)
    state = reduceBootstrapState(state, { type: 'stage', name: 'python-deps', state: 'running' }, 1_100)
    state = reduceBootstrapState(
      state,
      {
        type: 'progress',
        stage: 'python-deps',
        completed: 64,
        total: 128,
        unit: 'bytes',
        label: 'Dependency archive'
      },
      1_200
    )

    const first = safeBootstrapState(state)
    const restored = structuredClone(first)

    assert.deepEqual(restored, first)
    assert.equal(restored.stages['python-deps']?.startedAt, 1_100)
    assert.equal(restored.stages['python-deps']?.progress?.total, 128)
    assert.equal(restored.stages['python-deps']?.progress?.updatedAt, 1_200)
  })
})
