import assert from 'node:assert/strict'
import fs from 'node:fs'
import { mkdtemp, rm, stat } from 'node:fs/promises'
import http from 'node:http'
import { join } from 'node:path'

import { test } from 'vitest'

import {
  DEFAULT_TRACE_UPSTREAM_URL,
  isExpectedTraceShutdownError,
  RefreshingTraceCredentialProvider,
  type TraceCredentialSource,
  TraceForwarder
} from './trace-forwarder'
import { createSafeStorageTraceKeyProtector } from './trace-outbox-crypto'
import { nodeTraceFileSystem, TraceJournal } from './trace-outbox-journal'
import { TraceOutboxStore } from './trace-outbox-store'
import type { DurableReceipt, DurableTraceBatch, TraceEnvelopeInput, TraceOwner } from './trace-outbox-types'
import { TraceRecoveryController, TraceRecoveryLifecycle } from './trace-recovery-controller'

const installationId = '11111111-1111-4111-8111-111111111111'
const protobuf = Buffer.from([0x0a, 0x03, 0x01, 0x02, 0x03])
const traceCredentialNow = Date.parse('2099-08-23T14:00:00+00:00')

function validOwner(): TraceOwner {
  return {
    accountId: '11111111-1111-4111-8111-111111111111',
    accountKey: 'account-11111111-1111-4111-8111-111111111111',
    installationId,
    sessionId: '22222222-2222-4222-8222-222222222222'
  }
}

function legacyOwner(): TraceOwner {
  return {
    accountId: null,
    accountKey: `legacy-${'a'.repeat(64)}`,
    installationId,
    sessionId: null
  }
}

function keyProtector() {
  return createSafeStorageTraceKeyProtector({
    decryptString: ciphertext => ciphertext.toString('utf8'),
    encryptString: plaintext => Buffer.from(plaintext, 'utf8'),
    isEncryptionAvailable: () => true
  })
}

async function temporaryStore() {
  const root = await mkdtemp(join(process.cwd(), 'tmp', 'trace-forwarder-'))
  const store = await TraceOutboxStore.open({
    expectedOwner: validOwner(),
    groupCommitMs: 1,
    keyProtector: keyProtector(),
    root
  })

  return { root, store }
}

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(currentResolve => {
    resolve = currentResolve
  })

  return { promise, resolve }
}

function envelope(label: string): TraceEnvelopeInput {
  return {
    body: Buffer.from(`trace:${label}`),
    contentType: 'application/x-protobuf',
    entrypoint: 'desktop',
    hermesSessionId: `session-${label}`,
    owner: validOwner(),
    runId: `run-${label}`,
    telemetrySchemaVersion: '1'
  }
}

function receiptSyncFailureStore(batchId: string, phase: 'before' | 'after') {
  const durable = new Promise<DurableTraceBatch>(() => {})

  return {
    async acknowledge(_batchId: string, _receipt: DurableReceipt): Promise<void> {},
    beginEnqueue: () => ({
      batchId,
      cancelForGatewayReceipt: async () => {
        throw new Error(`receipt_sync_${phase}_local_commit_failed`)
      },
      durable
    }),
    diagnostics: async () => emptyDiagnostics(),
    async peekEligible(_now: number): Promise<DurableTraceBatch | undefined> {
      return undefined
    },
    async quarantine(_batchId: string, _errorClass: string): Promise<void> {},
    async quarantineInput(_input: TraceEnvelopeInput, _errorClass: string): Promise<DurableTraceBatch> {
      throw new Error('unexpected_quarantine')
    }
  }
}

function emptyDiagnostics() {
  return {
    accepted: 0,
    deduplicated: 0,
    duplicate: 0,
    evictedCapacity: 0,
    expired: 0,
    keyLost: 0,
    payloadBytes: 0,
    pending: 0,
    pendingBytes: 0,
    quarantined: 0,
    recoveredCorruptTail: 0,
    tombstoneBytes: 0,
    tombstones: 0
  }
}

test('product Trace uploads use the public same-origin Gateway API by default', () => {
  assert.equal(DEFAULT_TRACE_UPSTREAM_URL, 'https://c2sml.cn/trace-ingest/v1/traces')
})

test('a matching Gateway receipt owns a trace before local fsync without retaining payload bytes', async () => {
  const root = await mkdtemp(join(process.cwd(), 'tmp', 'trace-forwarder-gateway-first-'))
  const store = await TraceOutboxStore.open({
    expectedOwner: validOwner(),
    groupCommitMs: 50,
    keyProtector: keyProtector(),
    root
  })
  const source = credentialSource()
  const upstream: Array<{ body: Buffer; headers: Headers }> = []

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(source, { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      const headers = new Headers(init?.headers)
      upstream.push({ body: Buffer.from(init?.body as Buffer), headers })

      return new Response(Buffer.alloc(0), {
        status: 200,
        headers: {
          'x-trace-batch-id': headers.get('idempotency-key') ?? '',
          'x-trace-receipt': 'accepted'
        }
      })
    },
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    assert.deepEqual(source.calls, [])
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    await waitFor(() => upstream.length === 1)
    assert.equal(upstream[0].headers.get('idempotency-key')?.length, 36)
    assert.match(upstream[0].headers.get('x-trace-payload-sha256') ?? '', /^[a-f0-9]{64}$/)
    assert.deepEqual(upstream[0].body, protobuf)
    assert.equal((await store.diagnostics()).payloadBytes, 0)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('an unresolved migration barrier keeps capture durable and prevents direct or pumped cloud upload', async () => {
  const { root, store } = await temporaryStore()
  const migration = deferred<void>()
  const upstream: string[] = []
  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      upstream.push(Buffer.from(init?.body as Buffer).toString('hex'))
      const headers = new Headers(init?.headers)
      return new Response(Buffer.alloc(0), {
        status: 200,
        headers: { 'x-trace-batch-id': headers.get('idempotency-key') ?? '', 'x-trace-receipt': 'accepted' }
      })
    },
    installationId,
    store,
    uploadBarrier: () => migration.promise
  })
  const started = await forwarder.start(validOwner())

  try {
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    assert.equal((await store.diagnostics()).pending, 1)
    assert.deepEqual(upstream, [])

    migration.resolve()
    await forwarder.pump()
    assert.deepEqual(upstream, [protobuf.toString('hex')])
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test.each(['before', 'after'] as const)(
  'a %s-local-commit receipt sync failure does not falsely acknowledge the Gateway winner',
  async phase => {
    const batchId = '00000000-0000-4000-8000-000000000010'

    const forwarder = new TraceForwarder({
      credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), {
        clock: () => traceCredentialNow
      }),
      fetchImpl: async (_input, init) => {
        const headers = new Headers(init?.headers)

        return new Response(Buffer.alloc(0), {
          status: 200,
          headers: { 'x-trace-batch-id': headers.get('idempotency-key') ?? '', 'x-trace-receipt': 'accepted' }
        })
      },
      installationId,
      store: receiptSyncFailureStore(batchId, phase)
    })

    const started = await forwarder.start(validOwner())

    try {
      const response = await post(started.endpoint, started.localBearer)
      assert.equal(response.status, 503)
      assert.equal(response.headers['content-type'], 'application/x-protobuf')
      assert.equal(response.headers['retry-after'], '1')
      assert.deepEqual(response.body.subarray(0, 2), Buffer.from([0x08, 0x0e]))
    } finally {
      await forwarder.stop({ flushMs: 0 })
    }
  }
)

test('persists normal, oversized, and later normal OTLP parts in source journal order', async () => {
  const { root, store } = await temporaryStore()

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => new Response(Buffer.alloc(0), { status: 503 }),
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    assert.equal(
      (
        await post(started.endpoint, started.localBearer, {
          body: otlpSequencePayload()
        })
      ).status,
      413
    )
    const journal = await TraceJournal.open({ fs: nodeTraceFileSystem, path: join(root, 'index.journal') })
    const operations = (await journal.recover()).operations

    assert.deepEqual(
      operations
        .filter(operation => operation.op === 'pending' || operation.op === 'terminal')
        .map(operation => operation.op),
      ['pending', 'pending', 'terminal', 'pending']
    )
    assert.deepEqual(
      operations.filter(operation => operation.op === 'pending').map(operation => operation.sequence),
      [0, 1, 2]
    )
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('an existing durable backlog disables direct Gateway upload until recovery drains it', async () => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('older'))
  let upstreamCalls = 0

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => {
      upstreamCalls += 1

      return new Response(Buffer.alloc(0), { status: 200 })
    },
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    assert.equal(upstreamCalls, 0)
    assert.equal((await store.diagnostics()).pending, 2)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('recovery pump uploads a durable account backlog in FIFO order', async () => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('older'))
  await store.enqueue(envelope('newer'))
  const uploaded: string[] = []

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      const headers = new Headers(init?.headers)
      uploaded.push(headers.get('x-trace-run-id') ?? '')

      return new Response(Buffer.alloc(0), {
        status: 200,
        headers: {
          'x-trace-batch-id': headers.get('idempotency-key') ?? '',
          'x-trace-receipt': 'accepted'
        }
      })
    },
    installationId,
    store
  })

  await forwarder.start(validOwner())

  try {
    await forwarder.pump()
    assert.deepEqual(uploaded, ['run-older', 'run-newer'])
    assert.equal((await store.diagnostics()).pending, 0)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('recovery pump honors retry backoff without dropping the durable FIFO head', async () => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('retry'))
  let now = traceCredentialNow
  let calls = 0

  const forwarder = new TraceForwarder({
    clock: () => now,
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      calls += 1
      const headers = new Headers(init?.headers)

      return calls === 1
        ? new Response(Buffer.alloc(0), { status: 503, headers: { 'retry-after': '120' } })
        : new Response(Buffer.alloc(0), {
            status: 200,
            headers: {
              'x-trace-batch-id': headers.get('idempotency-key') ?? '',
              'x-trace-receipt': 'accepted'
            }
          })
    },
    installationId,
    random: () => 0,
    store
  })

  await forwarder.start(validOwner())

  try {
    await forwarder.pump()
    assert.equal(calls, 1)
    assert.equal(forwarder.nextRecoveryAt(), traceCredentialNow + 120_000)
    assert.equal((await store.diagnostics()).pending, 1)

    await forwarder.pump()
    assert.equal(calls, 1)

    now += 120_000
    await forwarder.pump()
    assert.equal(calls, 2)
    assert.equal(forwarder.nextRecoveryAt(), null)
    assert.equal((await store.diagnostics()).pending, 0)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('a matching structured terminal 403 pauses its owner exactly once without a recovery hot loop', async () => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('revoked'))
  const revocations: unknown[] = []
  let calls = 0

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => {
      calls += 1

      return new Response(
        JSON.stringify({
          account_id: validOwner().accountId,
          code: 'session_revoked',
          retryable: false,
          revoked_at: '2099-08-23T14:00:00Z',
          session_id: validOwner().sessionId,
          state: 'revoked'
        }),
        { status: 403, headers: { 'content-type': 'application/json' } }
      )
    },
    installationId,
    onTerminalRevocation: revocation => revocations.push(revocation),
    store
  })

  await forwarder.start(validOwner())

  try {
    await forwarder.pump()
    await forwarder.pump()

    assert.equal(calls, 1)
    assert.equal((await store.diagnostics()).pending, 1)
    assert.deepEqual(revocations, [
      {
        accountId: validOwner().accountId,
        code: 'session_revoked',
        revokedAt: '2099-08-23T14:00:00Z',
        sessionId: validOwner().sessionId
      }
    ])
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test.each([
  ['unstructured', { error: 'forbidden' }],
  [
    'owner-mismatched',
    {
      account_id: '33333333-3333-4333-8333-333333333333',
      code: 'account_revoked',
      retryable: false,
      revoked_at: '2099-08-23T14:00:00Z',
      session_id: validOwner().sessionId,
      state: 'revoked'
    }
  ]
])('an %s 403 is transient and retains the FIFO head behind backoff', async (_label, body) => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('ordinary-403'))
  const revocations: unknown[] = []
  let calls = 0

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => {
      calls += 1

      return new Response(JSON.stringify(body), { status: 403 })
    },
    installationId,
    onTerminalRevocation: revocation => revocations.push(revocation),
    random: () => 0,
    store
  })

  await forwarder.start(validOwner())

  try {
    await forwarder.pump()
    await forwarder.pump()

    assert.equal(calls, 1)
    assert.notEqual(forwarder.nextRecoveryAt(), null)
    assert.equal((await store.diagnostics()).pending, 1)
    assert.deepEqual(revocations, [])
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('recovery drops an obsolete retry timer after its durable FIFO head becomes terminal', async () => {
  const { root, store } = await temporaryStore()
  const batch = await store.enqueue(envelope('terminal-during-retry'))

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => new Response(Buffer.alloc(0), { status: 503 }),
    installationId,
    store
  })

  await forwarder.start(validOwner())

  try {
    await forwarder.pump()
    assert.notEqual(forwarder.nextRecoveryAt(), null)

    await store.quarantine(batch.batchId, 'capacity')
    await forwarder.pump()
    assert.equal(forwarder.nextRecoveryAt(), null)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('stopping an owner prevents a late Trace credential from uploading its durable backlog', async () => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('stopped-owner'))

  const token = deferred<{
    access_token: string
    expires_at: string
    expires_in: number
    installation_id: string
  }>()

  let credentialLoads = 0
  let upstreamCalls = 0

  const provider = new RefreshingTraceCredentialProvider(
    {
      async load() {
        credentialLoads += 1

        return token.promise
      }
    },
    { clock: () => traceCredentialNow }
  )

  const forwarder = new TraceForwarder({
    credentialProvider: provider,
    fetchImpl: async () => {
      upstreamCalls += 1

      return new Response(Buffer.alloc(0), { status: 200 })
    },
    installationId,
    store
  })

  await forwarder.start(validOwner())

  try {
    const pump = forwarder.pump()
    await waitFor(() => credentialLoads === 1)
    await forwarder.stop({ flushMs: 0 })
    token.resolve({
      access_token: 'public-trace-token-late-1234567890',
      expires_at: '2099-08-23T14:15:00+00:00',
      expires_in: 900,
      installation_id: installationId
    })
    await pump

    assert.equal(upstreamCalls, 0)
    assert.equal((await store.diagnostics()).pending, 1)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('stop fences a late Gateway response before it can mutate the closed owner outbox', async () => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('late-response'))
  const gateway = deferred<Response>()
  let batchId = ''

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      batchId = new Headers(init?.headers).get('idempotency-key') ?? ''

      return gateway.promise
    },
    installationId,
    store
  })

  await forwarder.start(validOwner())
  const pump = forwarder.pump()

  try {
    await waitFor(() => batchId.length > 0)
    await forwarder.stop({ flushMs: 0 })
    const journalBefore = (await stat(join(root, 'index.journal'))).size

    gateway.resolve(
      new Response(Buffer.alloc(0), {
        status: 202,
        headers: { 'x-trace-batch-id': batchId, 'x-trace-receipt': 'accepted' }
      })
    )
    await pump

    assert.equal((await stat(join(root, 'index.journal'))).size, journalBefore)
    assert.equal((await store.diagnostics()).pending, 1)
  } finally {
    gateway.resolve(new Response(Buffer.alloc(0), { status: 503 }))
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('stop fences a late forced 401 refresh before retrying or mutating the closed owner outbox', async () => {
  const { root, store } = await temporaryStore()
  await store.enqueue(envelope('late-401-refresh'))

  const refreshed = deferred<{
    access_token: string
    expires_at: string
    expires_in: number
    installation_id: string
  }>()

  const forceRefreshCalls: boolean[] = []
  let upstreamCalls = 0

  const provider = new RefreshingTraceCredentialProvider(
    {
      async load(forceRefresh) {
        forceRefreshCalls.push(forceRefresh)

        if (forceRefresh) {
          return refreshed.promise
        }

        return {
          access_token: 'public-trace-token-initial-1234567890',
          expires_at: '2099-08-23T14:15:00+00:00',
          expires_in: 900,
          installation_id: installationId
        }
      }
    },
    { clock: () => traceCredentialNow }
  )

  const forwarder = new TraceForwarder({
    credentialProvider: provider,
    fetchImpl: async () => {
      upstreamCalls += 1

      return new Response(Buffer.alloc(0), { status: 401 })
    },
    installationId,
    store
  })

  await forwarder.start(validOwner())
  const pump = forwarder.pump()

  try {
    await waitFor(() => forceRefreshCalls.length === 2)
    await forwarder.stop({ flushMs: 0 })
    const journalBefore = (await stat(join(root, 'index.journal'))).size

    refreshed.resolve({
      access_token: 'public-trace-token-refreshed-1234567890',
      expires_at: '2099-08-23T14:15:00+00:00',
      expires_in: 900,
      installation_id: installationId
    })
    await pump

    assert.deepEqual(forceRefreshCalls, [false, true])
    assert.equal(upstreamCalls, 1)
    assert.equal((await stat(join(root, 'index.journal'))).size, journalBefore)
    assert.equal((await store.diagnostics()).pending, 1)
  } finally {
    refreshed.resolve({
      access_token: 'public-trace-token-refreshed-1234567890',
      expires_at: '2099-08-23T14:15:00+00:00',
      expires_in: 900,
      installation_id: installationId
    })
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('stop fences the detached direct-upload receipt continuation from the closed owner outbox', async () => {
  const { root, store } = await temporaryStore()
  const gateway = deferred<Response>()
  let batchId = ''

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      batchId = new Headers(init?.headers).get('idempotency-key') ?? ''

      return gateway.promise
    },
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    await waitFor(() => batchId.length > 0)
    await forwarder.stop({ flushMs: 0 })
    const journalBefore = (await stat(join(root, 'index.journal'))).size

    gateway.resolve(
      new Response(Buffer.alloc(0), {
        status: 202,
        headers: { 'x-trace-batch-id': batchId, 'x-trace-receipt': 'accepted' }
      })
    )
    await new Promise(resolve => setTimeout(resolve, 25))

    assert.equal((await stat(join(root, 'index.journal'))).size, journalBefore)
    assert.equal((await store.diagnostics()).pending, 1)
  } finally {
    gateway.resolve(new Response(Buffer.alloc(0), { status: 503 }))
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('a concurrent Trace cannot bypass the first direct durability contender', async () => {
  const { root, store } = await temporaryStore()
  const gateway = deferred<Response>()
  let upstreamCalls = 0

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      upstreamCalls += 1
      const headers = new Headers(init?.headers)

      return gateway.promise.then(
        () =>
          new Response(Buffer.alloc(0), {
            status: 200,
            headers: {
              'x-trace-batch-id': headers.get('idempotency-key') ?? '',
              'x-trace-receipt': 'accepted'
            }
          })
      )
    },
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    const first = post(started.endpoint, started.localBearer)
    await waitFor(() => upstreamCalls === 1)

    const second = post(started.endpoint, started.localBearer, {
      headers: { 'x-trace-run-id': 'run-2', 'x-hermes-session-id': 'session-2' }
    })

    gateway.resolve(new Response())
    assert.equal((await first).status, 200)
    assert.equal((await second).status, 200)
    assert.equal(upstreamCalls, 1)
    assert.equal((await store.diagnostics()).pending, 1)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('a permanent Gateway rejection quarantines the locally durable batch once', async () => {
  const { root, store } = await temporaryStore()

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => new Response(Buffer.alloc(0), { status: 409 }),
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    await waitFor(async () => (await store.diagnostics()).quarantined === 1)
    assert.equal((await store.diagnostics()).pending, 1)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

async function post(
  endpoint: string,
  localBearer: string,
  overrides: {
    body?: Buffer
    headers?: Record<string, string>
    includeCorrelationHeaders?: boolean
  } = {}
) {
  const target = new URL(endpoint)
  const body = overrides.body ?? protobuf

  const correlationHeaders =
    overrides.includeCorrelationHeaders === false
      ? {}
      : {
          'x-hermes-session-id': 'session-1',
          'x-trace-entrypoint': 'desktop',
          'x-trace-run-id': 'run-1',
          'x-telemetry-schema-version': '1'
        }

  const headers = {
    authorization: `Bearer ${localBearer}`,
    'content-type': 'application/x-protobuf',
    ...correlationHeaders,
    ...overrides.headers
  }

  return new Promise<{ body: Buffer; headers: http.IncomingHttpHeaders; status: number }>((resolve, reject) => {
    const request = http.request(
      {
        hostname: target.hostname,
        port: target.port,
        path: target.pathname,
        method: 'POST',
        headers
      },
      response => {
        const chunks: Buffer[] = []

        response.on('error', reject)
        response.on('data', chunk => chunks.push(Buffer.from(chunk)))
        response.on('end', () =>
          resolve({ body: Buffer.concat(chunks), headers: response.headers, status: response.statusCode ?? 0 })
        )
      }
    )

    request.on('error', reject)
    request.on('socket', socket => socket.on('error', reject))
    request.end(body)
  })
}

function varint(value: number): Buffer {
  const bytes: number[] = []
  let remaining = value

  do {
    const byte = remaining & 0x7f

    remaining = Math.floor(remaining / 128)
    bytes.push(remaining > 0 ? byte | 0x80 : byte)
  } while (remaining > 0)

  return Buffer.from(bytes)
}

function lengthDelimited(field: number, value: Buffer | string): Buffer {
  const body = typeof value === 'string' ? Buffer.from(value) : value

  return Buffer.concat([varint((field << 3) | 2), varint(body.length), body])
}

function otlpSequencePayload(): Buffer {
  const span = (id: string, bytes: number) =>
    lengthDelimited(
      1,
      lengthDelimited(
        2,
        lengthDelimited(2, Buffer.concat([lengthDelimited(1, id), lengthDelimited(2, Buffer.alloc(bytes))]))
      )
    )

  return Buffer.concat([span('normal-a', 1), span('oversized-b', 8 * 1024 * 1024), span('normal-c', 1)])
}

function keyValue(key: string, value: string): Buffer {
  const anyValue = lengthDelimited(1, value)

  return Buffer.concat([lengthDelimited(1, key), lengthDelimited(2, anyValue)])
}

function relayOtlpPayload(sessionId: string, traceIdHex: string): Buffer {
  const traceId = Buffer.from(traceIdHex, 'hex')
  const spanId = Buffer.from('1122334455667788', 'hex')
  const session = keyValue('nemo_relay.scope.metadata.session_id', sessionId)

  const span = Buffer.concat([
    lengthDelimited(1, traceId),
    lengthDelimited(2, spanId),
    lengthDelimited(5, 'hermes.turn'),
    lengthDelimited(9, session)
  ])

  const scopeSpans = lengthDelimited(2, span)
  const resourceSpans = lengthDelimited(2, scopeSpans)

  return lengthDelimited(1, resourceSpans)
}

async function waitFor(predicate: () => boolean | Promise<boolean>) {
  const deadline = Date.now() + 2_000

  while (!(await predicate())) {
    if (Date.now() >= deadline) {
      assert.fail('timed out waiting for forwarder')
    }

    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

function credentialSource(): TraceCredentialSource & { calls: boolean[] } {
  return {
    calls: [],
    async load(forceRefresh) {
      this.calls.push(forceRefresh)

      return {
        access_token: forceRefresh
          ? 'public-trace-token-refreshed-1234567890'
          : 'public-trace-token-initial-1234567890',
        expires_at: '2099-08-23T14:15:00+00:00',
        expires_in: 900,
        installation_id: installationId
      }
    }
  }
}

test('credential provider caches until 60 seconds before expiry and supports forced rotation', async () => {
  let now = Date.parse('2099-08-23T14:00:00+00:00')
  const source = credentialSource()
  const provider = new RefreshingTraceCredentialProvider(source, { clock: () => now })

  const first = await provider.current()
  assert.equal(await provider.current(), first)
  now = Date.parse(first.expires_at) - 60_000
  await provider.current()
  await provider.current({ forceRefresh: true })

  assert.deepEqual(source.calls, [false, false, true])
})

test('loopback forwarder accepts exact protobuf and adds only the public bearer upstream', async () => {
  const source = credentialSource()
  const calls: Array<{ body: Buffer; headers: Headers }> = []
  const { root, store } = await temporaryStore()

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(source, { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      calls.push({ body: Buffer.from(init?.body as Buffer), headers: new Headers(init?.headers) })

      return new Response(Buffer.alloc(0), { status: 200 })
    },
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    const response = await post(started.endpoint, started.localBearer)
    assert.equal(response.status, 200)
    await waitFor(() => calls.length === 1)
    assert.deepEqual(calls[0].body, protobuf)
    assert.equal(calls[0].headers.get('authorization'), 'Bearer public-trace-token-initial-1234567890')
    assert.equal(calls[0].headers.get('x-hermes-session-id'), 'session-1')
    assert.equal(calls[0].headers.get('x-trace-run-id'), 'run-1')
    assert.equal(calls[0].headers.has('x-local-authorization'), false)
  } finally {
    await forwarder.stop({ flushMs: 3_000 })
    await rm(root, { force: true, recursive: true })
  }
})

test('real Relay OTLP without custom correlation headers is canonicalized for the Gateway', async () => {
  const calls: Headers[] = []
  const traceId = '00112233445566778899aabbccddeeff'
  const { root, store } = await temporaryStore()

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      calls.push(new Headers(init?.headers))

      return new Response(Buffer.alloc(0), { status: 200 })
    },
    installationId,
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    const response = await post(started.endpoint, started.localBearer, {
      body: relayOtlpPayload('desktop-session-real', traceId),
      includeCorrelationHeaders: false
    })

    assert.equal(response.status, 200)
    await waitFor(() => calls.length === 1)
    assert.equal(calls[0].get('x-hermes-session-id'), 'desktop-session-real')
    assert.equal(calls[0].get('x-trace-run-id'), traceId)
    assert.equal(calls[0].get('x-trace-entrypoint'), 'desktop')
    assert.equal(calls[0].get('x-telemetry-schema-version'), '1')
  } finally {
    await forwarder.stop({ flushMs: 3_000 })
    await rm(root, { force: true, recursive: true })
  }
})

test('one upstream 401 forces one credential refresh and resends identical bytes once', async () => {
  const source = credentialSource()
  const bodies: Buffer[] = []
  const authorizations: string[] = []
  const recoveryReasons: string[] = []

  const provider = new RefreshingTraceCredentialProvider(source, { clock: () => traceCredentialNow })
  const { root, store } = await temporaryStore()
  const invalidate = provider.invalidate.bind(provider)

  let invalidations = 0

  provider.invalidate = () => {
    invalidations += 1
    invalidate()
  }

  const forwarder = new TraceForwarder({
    credentialProvider: provider,
    fetchImpl: async (_input, init) => {
      bodies.push(Buffer.from(init?.body as Buffer))
      authorizations.push(new Headers(init?.headers).get('authorization') ?? '')

      const headers = new Headers(init?.headers)

      return new Response(Buffer.alloc(0), {
        status: bodies.length === 1 ? 401 : 200,
        headers:
          bodies.length === 1
            ? {}
            : {
                'x-trace-batch-id': headers.get('idempotency-key') ?? '',
                'x-trace-receipt': 'accepted'
              }
      })
    },
    installationId,
    recovery: { trigger: reason => recoveryReasons.push(reason) },
    store
  })

  const started = await forwarder.start(validOwner())

  try {
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    await waitFor(() => bodies.length === 2)
    assert.deepEqual(bodies[0], bodies[1])
    assert.deepEqual(authorizations, [
      'Bearer public-trace-token-initial-1234567890',
      'Bearer public-trace-token-refreshed-1234567890'
    ])
    assert.deepEqual(source.calls, [false, true])
    assert.equal(invalidations, 2)
    assert.deepEqual(recoveryReasons, ['upload-401', 'token-ready'])
  } finally {
    await forwarder.stop({ flushMs: 3_000 })
    await rm(root, { force: true, recursive: true })
  }
})

test('HTTP boundary rejects remote peers, bad local auth, media drift, encoding, oversize, and stale epoch', async () => {
  let remoteAddress = '127.0.0.1'
  let upstreamCalls = 0
  const { root, store } = await temporaryStore()

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => {
      upstreamCalls += 1

      return new Response(Buffer.alloc(0), { status: 200 })
    },
    installationId,
    remoteAddressForRequest: () => remoteAddress,
    store
  })

  const first = await forwarder.start(validOwner())

  remoteAddress = '192.0.2.10'
  assert.equal((await post(first.endpoint, first.localBearer)).status, 403)
  remoteAddress = '127.0.0.1'
  assert.equal((await post(first.endpoint, 'wrong-local-bearer')).status, 401)
  assert.equal(
    (await post(first.endpoint, first.localBearer, { headers: { 'content-type': 'application/json' } })).status,
    415
  )
  assert.equal((await post(first.endpoint, first.localBearer, { headers: { 'content-encoding': 'gzip' } })).status, 415)
  assert.equal((await post(first.endpoint, first.localBearer, { body: Buffer.alloc(8 * 1024 * 1024 + 1) })).status, 413)
  await forwarder.stop({ flushMs: 0 })

  const second = await forwarder.start(validOwner())

  try {
    assert.equal((await post(second.endpoint, first.localBearer)).status, 401)
    assert.equal(upstreamCalls, 0)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})

test('stop remains bounded when a local OTLP client holds an incomplete request open', async () => {
  const { root, store } = await temporaryStore()
  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource()),
    installationId,
    store
  })
  const started = await forwarder.start(validOwner())
  const target = new URL(started.endpoint)
  const request = http.request({
    hostname: target.hostname,
    port: target.port,
    path: target.pathname,
    method: 'POST',
    headers: {
      authorization: `Bearer ${started.localBearer}`,
      'content-length': '5',
      'content-type': 'application/x-protobuf'
    }
  })
  request.on('error', () => {})
  const connected = new Promise<void>(resolve => {
    request.on('socket', socket => {
      if (socket.readyState === 'open') {
        resolve()
      } else {
        socket.once('connect', resolve)
      }
    })
  })
  request.flushHeaders()
  await connected

  const stopping = forwarder.stop({ flushMs: 0 })

  try {
    const stoppedWithinBoundary = await Promise.race([
      stopping.then(() => true),
      new Promise<false>(resolve => setTimeout(() => resolve(false), 250))
    ])
    assert.equal(stoppedWithinBoundary, true)
  } finally {
    request.destroy()
    await stopping
    await rm(root, { force: true, recursive: true })
  }
})

test('stop consumes only expected socket errors with oversized and held requests', async () => {
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('reset'), { code: 'ECONNRESET' }), true), true)
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('pipe'), { code: 'EPIPE' }), true), true)
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('reset'), { code: 'ECONNRESET' }), false), false)
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('other'), { code: 'ENOSPC' }), true), false)

  const { root, store } = await temporaryStore()
  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource()),
    installationId,
    maxBodyBytes: 4,
    store
  })
  const started = await forwarder.start(validOwner())
  const target = new URL(started.endpoint)
  const requests = [Buffer.alloc(5), Buffer.from([0x0a])].map((body, index) => {
    const request = http.request({
      hostname: target.hostname,
      port: target.port,
      path: target.pathname,
      method: 'POST',
      headers: {
        authorization: `Bearer ${started.localBearer}`,
        ...(index === 0 ? {} : { 'content-length': '5' }),
        'content-type': 'application/x-protobuf'
      }
    })
    request.on('error', error => {
      assert.ok(['ECONNRESET', 'EPIPE'].includes((error as NodeJS.ErrnoException).code ?? ''))
    })
    request.on('socket', socket => {
      socket.on('error', error => {
        assert.ok(['ECONNRESET', 'EPIPE'].includes((error as NodeJS.ErrnoException).code ?? ''))
      })
    })
    request.write(body)
    request.flushHeaders()

    return request
  })

  const stopping = forwarder.stop({ flushMs: 0 })

  try {
    assert.equal(
      await Promise.race([
        stopping.then(() => true),
        new Promise<false>(resolve => setTimeout(() => resolve(false), 250))
      ]),
      true
    )
  } finally {
    for (const request of requests) {
      request.destroy()
    }
    await stopping
    await rm(root, { force: true, recursive: true })
  }
})

test('desktop lifecycle starts local backend through degraded Trace recovery and stops Trace before teardown', () => {
  const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
  const prepareStart = source.indexOf('prepareLocalBackend: async () => {')
  const prepareEnd = source.indexOf('resolveRemote:', prepareStart)
  const prepare = source.slice(prepareStart, prepareEnd)

  assert.ok(prepareStart >= 0)
  assert.match(prepare, /return resolveLocalBackendWithTrace\(\{/)
  assert.match(prepare, /resolveBackend: \(\) => resolveHermesBackend\(backendArgs\)/)
  assert.match(prepare, /startEncryptedTrace: \(\) => ensureDesktopTraceForwarder\(connectionScope, owner\)/)

  const cleanupStart = source.indexOf("async function cleanupDesktopCapabilities(connectionId = 'local')")
  const cleanupEnd = source.indexOf('function enableDesktopCapabilityShell()', cleanupStart)
  const cleanup = source.slice(cleanupStart, cleanupEnd)

  assert.ok(cleanupStart >= 0)
  assert.ok(
    cleanup.indexOf('await stopDesktopTraceForwarder(3_000)') <
      cleanup.indexOf('teardownPrimaryBackendAndWait({ soft: true })')
  )
  assert.match(source, /trace: traceContextForBackendRoot\(root\)/)
  assert.match(source, /trace: traceContextForBackendRoot\(ACTIVE_HERMES_ROOT\)/)
  assert.match(source, /pluginsToml: path\.join\(root, 'config', 'ansatz-voice-trace', 'plugins\.toml'\)/)
})

test('local backend preparation does not wait for Trace token acquisition', async () => {
  const token = deferred<Awaited<ReturnType<TraceCredentialSource['load']>>>()
  let tokenLoads = 0
  let tokenResolved = false

  const provider = new RefreshingTraceCredentialProvider(
    {
      load: async () => {
        tokenLoads += 1
        const credential = await token.promise
        tokenResolved = true

        return credential
      }
    },
    { clock: () => traceCredentialNow }
  )

  const root = await mkdtemp(join(process.cwd(), 'tmp', 'trace-forwarder-token-independent-'))
  const store = await TraceOutboxStore.open({
    expectedOwner: legacyOwner(),
    groupCommitMs: 1,
    keyProtector: keyProtector(),
    root
  })
  const forwarder = new TraceForwarder({ credentialProvider: provider, installationId, store })

  const controller = new TraceRecoveryController({
    accountKey: legacyOwner().accountKey,
    pump: () => forwarder.pump()
  })

  const lifecycle = new TraceRecoveryLifecycle({ controller, credentialProvider: provider })

  try {
    const started = await forwarder.start(legacyOwner())
    lifecycle.start()
    const backend = await Promise.resolve({ kind: 'local' as const })

    assert.equal(backend.kind, 'local')
    assert.equal(tokenLoads, 1)
    assert.equal(tokenResolved, false)
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    assert.equal(tokenResolved, false)

    await forwarder.pump()
    assert.equal(tokenResolved, false)
    assert.equal((await store.diagnostics()).pending, 1)
  } finally {
    await lifecycle.stop()
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { force: true, recursive: true })
  }
})
