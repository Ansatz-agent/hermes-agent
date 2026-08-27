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
  cloud_state: 'active',
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

test('renderer status preserves degraded cloud state after the local runtime is ready', async () => {
  const gate = new DesktopRuntimeGate()
  await gate.prepare(async () => {})

  const status = gate.rendererStatus({
    ...authenticated,
    valid_until: 0,
    cloud_state: 'unreachable',
    reason: 'server_unavailable'
  })

  assert.equal(status.state, 'authenticated')
  assert.equal(status.cloud_state, 'unreachable')
  assert.equal(status.runtime_ready, true)
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

test('auth status refresh does not tear down an already trusted desktop runtime', () => {
  const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')

  const authSubscription = source.slice(
    source.indexOf('coordinator.subscribe((status, connectionId) => {'),
    source.indexOf('\n      try {', source.indexOf('coordinator.subscribe((status, connectionId) => {'))
  )

  assert.doesNotMatch(authSubscription, /cleanupDesktopCapabilities|desktopRuntimeGate\.invalidate/)
})

test('authenticated owner refresh rebinds Trace even while the macOS main window is closed', () => {
  const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')

  const subscription = source.slice(
    source.indexOf('coordinator.subscribe((status, connectionId) => {'),
    source.indexOf('\n      try {', source.indexOf('coordinator.subscribe((status, connectionId) => {'))
  )

  const traceSync = subscription.indexOf('void prepareDesktopTraceForwarder(traceScope, traceOwner)')
  const windowGate = subscription.indexOf('if (mainWindow && !mainWindow.isDestroyed())')

  assert.ok(traceSync >= 0)
  assert.ok(windowGate >= 0)
  assert.ok(traceSync < windowGate, 'Trace owner rebind must not depend on a live renderer window')
})

test('Trace durability keeps backend publication, idle compaction, revocation, and cleanup-order wiring', () => {
  const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
  const cleanupStart = source.indexOf("async function cleanupDesktopCapabilities(connectionId = 'local')")
  const cleanupEnd = source.indexOf('function enableDesktopCapabilityShell()', cleanupStart)
  const cleanup = source.slice(cleanupStart, cleanupEnd)

  assert.match(source, /trace: traceContextForBackendRoot\(root\)/)
  assert.match(source, /trace: traceContextForBackendRoot\(ACTIVE_HERMES_ROOT\)/)
  assert.match(source, /onTerminalRevocation: revocation =>[\s\S]*?applyTraceTerminalRevocation\(revocation\)/)
  assert.match(
    source,
    /activeWorkByWebContents\.set\(id, normalizeActiveWork\(payload\)\)[\s\S]*?compactDesktopTraceOutboxIfIdle\(\)/
  )
  assert.ok(
    cleanup.indexOf('await stopDesktopTraceForwarder(3_000)') <
      cleanup.indexOf('teardownPrimaryBackendAndWait({ soft: true })')
  )
  assert.match(source, /!isTraceDurabilityStartupError\(error\)/)
})

test('main records real auth owner transitions and migrates them in the background after backend readiness is unblocked', () => {
  const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')

  assert.match(source, /traceMigrationSourceOwner\(status, desktopInstallationId\)/)
  assert.match(source, /const migrationBarrier =[\s\S]*?store\.migrateTrustedSource\(/)
  assert.match(source, /void migrationBarrier\.catch\(/)
  assert.match(source, /uploadBarrier: migrationBarrier === null \? undefined : \(\) => migrationBarrier/)
  assert.doesNotMatch(source, /await TraceOutboxStore\.migrateTrustedNamespace\(/)
})
