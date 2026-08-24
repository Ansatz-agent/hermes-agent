import assert from 'node:assert/strict'
import fs from 'node:fs'

import { test } from 'vitest'

import { DesktopRuntimeGate } from './desktop-runtime-gate'

const authenticated = {
  state: 'authenticated',
  username: 'alice',
  runtime_instance_id: 'runtime',
  epoch: 1,
  valid_until: 60,
  session_expires_at: null,
  reason: null
}

test('renderer status reports installed runtime readiness across sign-in and sign-out', async () => {
  const gate = new DesktopRuntimeGate()

  assert.equal(gate.rendererStatus(authenticated).runtime_ready, false)
  await gate.prepare(async () => {})
  assert.equal(gate.rendererStatus(authenticated).runtime_ready, true)
  assert.equal(gate.rendererStatus({ ...authenticated, state: 'signed_out' }).runtime_ready, true)
  assert.equal(gate.ready, true)
})

test('runtime preparation is single-flight', async () => {
  const gate = new DesktopRuntimeGate()
  let release: (() => void) | null = null
  let calls = 0

  const task = () => {
    calls += 1

    return new Promise<void>(resolve => {
      release = resolve
    })
  }

  const first = gate.prepare(task)
  const second = gate.prepare(task)

  assert.equal(calls, 1)
  release?.()
  await Promise.all([first, second])
  assert.equal(gate.ready, true)
})

test('invalidating an in-flight preparation prevents stale readiness', async () => {
  const gate = new DesktopRuntimeGate()
  let release: (() => void) | null = null

  const pending = gate.prepare(
    () =>
      new Promise<void>(resolve => {
        release = resolve
      })
  )

  gate.invalidate()
  release?.()

  await assert.rejects(pending, /RUNTIME_PREPARATION_CANCELLED/)
  assert.equal(gate.ready, false)
  assert.equal(gate.state, 'not-ready')
})

test('failed preparation remains terminal and retryable', async () => {
  const gate = new DesktopRuntimeGate()

  await assert.rejects(
    gate.prepare(async () => Promise.reject(new Error('boom'))),
    /boom/
  )
  assert.equal(gate.state, 'failed')

  await gate.prepare(async () => {})
  assert.equal(gate.state, 'ready')
})

test('local backend startup waits for capture setup but never a cloud Trace credential', () => {
  const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')

  const traceStart = source.slice(
    source.indexOf('async function ensureDesktopTraceForwarder(scope) {'),
    source.indexOf(
      'async function stopDesktopTraceForwarder',
      source.indexOf('async function ensureDesktopTraceForwarder(scope) {')
    )
  )

  const primaryPreparation = source.slice(
    source.indexOf('prepareLocalBackend: async () => {'),
    source.indexOf('resolveRemote:', source.indexOf('prepareLocalBackend: async () => {'))
  )

  const poolPreparation = source.slice(
    source.indexOf('async function spawnPoolBackend(profile, entry) {'),
    source.indexOf(
      '// Same update mutual exclusion',
      source.indexOf('async function spawnPoolBackend(profile, entry) {')
    )
  )

  const authSubscription = source.slice(
    source.indexOf('coordinator.subscribe((status, connectionId) => {'),
    source.indexOf('\n      try {', source.indexOf('coordinator.subscribe((status, connectionId) => {'))
  )

  assert.doesNotMatch(source, /await provider\.current\(\)/)
  assert.match(traceStart, /await desktopTraceCapture\.prepare\(scope\)/)
  assert.match(source, /new LocalTraceCaptureController/)
  assert.match(source, /const TRACE_CAPTURE_SETUP_TIMEOUT_MS = \d+/)
  assert.match(primaryPreparation, /await prepareTraceCaptureForLocalScope\(connectionScope\)/)
  assert.match(poolPreparation, /await prepareTraceCaptureForLocalScope\(connectionScope\)/)
  assert.doesNotMatch(primaryPreparation, /await ensureDesktopTraceForwarder\(connectionScope\)/)
  assert.doesNotMatch(poolPreparation, /await ensureDesktopTraceForwarder\(connectionScope\)/)
  assert.doesNotMatch(authSubscription, /cleanupDesktopCapabilities|desktopRuntimeGate\.invalidate/)
})
