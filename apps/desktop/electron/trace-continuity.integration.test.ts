import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises'
import http from 'node:http'
import { join } from 'node:path'

import { test } from 'vitest'

import { RefreshingTraceCredentialProvider, TraceForwarder } from './trace-forwarder'
import { createSafeStorageTraceKeyProtector, type TraceKeyProtector } from './trace-outbox-crypto'
import { nodeTraceFileSystem, type TraceFileSystem } from './trace-outbox-journal'
import { TraceOutboxStore } from './trace-outbox-store'
import type { TraceEnvelopeInput, TraceOwner } from './trace-outbox-types'
import { TraceRecoveryController, TraceRecoveryLifecycle } from './trace-recovery-controller'

const installationId = '11111111-1111-4111-8111-111111111111'

function owner(account: 'a' | 'b'): TraceOwner {
  const accountId = account === 'a' ? 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' : 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

  return {
    accountId,
    accountKey: `account-${accountId}`,
    installationId,
    sessionId: account === 'a' ? 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa' : 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb'
  }
}

function payload(label: string): Buffer {
  return Buffer.from(`otlp-continuity-fixture:${label}:${'payload-not-plaintext-on-disk'.repeat(4)}`, 'utf8')
}

function sha256(body: Buffer): string {
  return createHash('sha256').update(body).digest('hex')
}

function protector(): TraceKeyProtector {
  return createSafeStorageTraceKeyProtector({
    decryptString: ciphertext => {
      const wrapped = ciphertext.toString('utf8')

      if (!wrapped.startsWith('test-safe-storage:')) {
        throw new Error('encryption_unavailable')
      }

      return wrapped.slice('test-safe-storage:'.length)
    },
    encryptString: plaintext => Buffer.from(`test-safe-storage:${plaintext}`, 'utf8'),
    isEncryptionAvailable: () => true
  })
}

function lostProtector(): TraceKeyProtector {
  return createSafeStorageTraceKeyProtector({
    decryptString: () => {
      throw new Error('encryption_unavailable')
    },
    encryptString: plaintext => Buffer.from(`wrong-account-key:${plaintext}`, 'utf8'),
    isEncryptionAvailable: () => true
  })
}

type GatewayMode = 'offline' | 'online'

type GatewayAttempt = {
  batchId: string
  body: Buffer
  digest: string
  outcome: 'accepted' | 'duplicate' | 'lost' | 'unavailable'
}

class ControllableGateway {
  static async start(mode: GatewayMode): Promise<ControllableGateway> {
    const gateway = new ControllableGateway(mode)
    await gateway.listen()

    return gateway
  }

  readonly attempts: GatewayAttempt[] = []
  readonly logical = new Map<string, { body: Buffer; digest: string }>()
  private loseResponses = 0
  private heldResponse: { received: () => void; release: Promise<void> } | null = null
  private readonly server: http.Server
  private constructor(private mode: GatewayMode) {
    this.server = http.createServer((request, response) => void this.handle(request, response))
  }

  get endpoint(): string {
    const address = this.server.address()

    if (!address || typeof address === 'string') {
      throw new Error('gateway_not_listening')
    }

    return `http://127.0.0.1:${address.port}/trace-ingest/v1/traces`
  }

  get logicalBatchCount(): number {
    return this.logical.size
  }

  loseNextResponse(): void {
    this.loseResponses += 1
  }

  holdNextResponse(): { received: Promise<void>; release(): void } {
    let markReceived!: () => void
    let release!: () => void
    const received = new Promise<void>(resolve => {
      markReceived = resolve
    })
    const released = new Promise<void>(resolve => {
      release = resolve
    })
    this.heldResponse = { received: markReceived, release: released }

    return { received, release }
  }

  setOnline(online: boolean): void {
    this.mode = online ? 'online' : 'offline'
  }

  async close(): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      this.server.close(error => (error ? reject(error) : resolve()))
    })
  }

  private async listen(): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      this.server.once('error', reject)
      this.server.listen(0, '127.0.0.1', () => {
        this.server.off('error', reject)
        resolve()
      })
    })
  }

  private async handle(request: http.IncomingMessage, response: http.ServerResponse): Promise<void> {
    const chunks: Buffer[] = []

    for await (const chunk of request) {
      chunks.push(Buffer.from(chunk))
    }

    const body = Buffer.concat(chunks)
    const batchId = singleHeader(request.headers['idempotency-key'])
    const digest = singleHeader(request.headers['x-trace-payload-sha256'])

    if (!batchId || !digest || digest !== sha256(body)) {
      response.writeHead(400, { 'content-length': '0' })
      response.end()

      return
    }

    if (this.mode === 'offline') {
      this.attempts.push({ batchId, body, digest, outcome: 'unavailable' })
      response.writeHead(503, { 'content-length': '0' })
      response.end()

      return
    }

    const existing = this.logical.get(batchId)

    if (existing && (existing.digest !== digest || !existing.body.equals(body))) {
      response.writeHead(409, { 'content-length': '0' })
      response.end()

      return
    }

    const outcome = existing ? 'duplicate' : 'accepted'
    this.logical.set(batchId, { body: Buffer.from(body), digest })

    const heldResponse = this.heldResponse
    if (heldResponse !== null) {
      this.heldResponse = null
      heldResponse.received()
      await heldResponse.release
    }

    if (this.loseResponses > 0) {
      this.loseResponses -= 1
      this.attempts.push({ batchId, body, digest, outcome: 'lost' })
      request.socket.destroy()

      return
    }

    this.attempts.push({ batchId, body, digest, outcome })
    response.writeHead(202, {
      'content-length': '0',
      'x-trace-batch-id': batchId,
      'x-trace-receipt': outcome
    })
    response.end()
  }
}

type TraceHarness = Awaited<ReturnType<typeof launchTraceHarness>>

async function launchTraceHarness(options: {
  fs?: TraceFileSystem
  gateway: ControllableGateway
  groupCommitMs?: number
  keyProtector?: TraceKeyProtector
  owner: TraceOwner
  receiptCapacityBytes?: number
  userData: string
}) {
  const root = join(options.userData, 'trace-outbox', options.owner.accountKey)

  const store = await TraceOutboxStore.open({
    expectedOwner: options.owner,
    fs: options.fs,
    groupCommitMs: options.groupCommitMs ?? 1,
    keyProtector: options.keyProtector ?? protector(),
    receiptCapacityBytes: options.receiptCapacityBytes,
    root
  })

  const credentialProvider = new RefreshingTraceCredentialProvider(
    {
      async load() {
        const expiresAt = Date.now() + 10 * 60_000

        return {
          access_token: 'integration-trace-token-abcdefghijklmnopqrstuvwxyz',
          expires_at: new Date(expiresAt).toISOString(),
          expires_in: 600,
          installation_id: installationId
        }
      }
    },
    { installationId }
  )

  let forwarder!: TraceForwarder
  let lifecycle!: TraceRecoveryLifecycle

  const controller = new TraceRecoveryController({
    accountKey: options.owner.accountKey,
    pump: async () => {
      await forwarder.pump()
      lifecycle.scheduleRetryAt(forwarder.nextRecoveryAt())
    }
  })

  forwarder = new TraceForwarder({
    credentialProvider,
    fetchImpl: fetch,
    installationId,
    recovery: controller,
    store,
    upstreamUrl: options.gateway.endpoint
  })
  const started = await forwarder.start(options.owner)
  lifecycle = new TraceRecoveryLifecycle({ controller, credentialProvider, periodicMs: 60 * 60_000 })
  lifecycle.start()

  return {
    endpoint: started.endpoint,
    localBearer: started.localBearer,
    owner: options.owner,
    root,
    store,
    async diagnostics() {
      return store.diagnostics()
    },
    async post(body: Buffer, sequence: number) {
      return postLoopback(started.endpoint, started.localBearer, body, sequence)
    },
    async quit() {
      await lifecycle.stop()
      const summary = await forwarder.stop({ flushMs: 3_000 })
      await controller.stop()

      return summary
    },
    async trigger() {
      controller.trigger('startup')
      await controller.whenIdle()
    }
  }
}

function envelopeFor(harness: TraceHarness, body: Buffer, sequence: number): TraceEnvelopeInput {
  return {
    body,
    contentType: 'application/x-protobuf',
    entrypoint: 'desktop',
    hermesSessionId: `session-${sequence}`,
    owner: harness.owner,
    runId: `run-${sequence}`,
    telemetrySchemaVersion: '1'
  }
}

function isActiveSegmentPath(path: string): boolean {
  return path.replaceAll('\\', '/').endsWith('/segments/active.segment')
}

function segmentSyncGate(): {
  fs: TraceFileSystem
  release(): void
  waitUntilBlocked(): Promise<void>
} {
  let release!: () => void
  let blocked!: () => void
  let released = false

  const releasePromise = new Promise<void>(resolve => {
    release = () => {
      released = true
      resolve()
    }
  })

  const blockedPromise = new Promise<void>(resolve => {
    blocked = resolve
  })

  return {
    fs: {
      ...nodeTraceFileSystem,
      async syncFile(path: string) {
        if (isActiveSegmentPath(path) && !released) {
          blocked()
          await releasePromise
        }

        await nodeTraceFileSystem.syncFile(path)
      }
    },
    release,
    waitUntilBlocked: () => blockedPromise
  }
}

test('segment fsync gate recognizes Windows path separators', () => {
  assert.equal(isActiveSegmentPath('C:\\user-data\\trace-outbox\\segments\\active.segment'), true)
  assert.equal(isActiveSegmentPath('C:\\user-data\\trace-outbox\\index.journal'), false)
})

async function temporaryUserData(): Promise<string> {
  const root = join(process.cwd(), 'tmp')
  await mkdir(root, { recursive: true })

  return mkdtemp(join(root, 'trace-continuity-'))
}

async function cleanupContinuityTest(
  harnesses: TraceHarness[],
  gateways: ControllableGateway[],
  userData: string
): Promise<void> {
  await Promise.allSettled(harnesses.map(harness => harness.quit()))
  await Promise.allSettled(gateways.map(gateway => gateway.close()))
  await rm(userData, { force: true, recursive: true }).catch(() => undefined)
}

test('quit waits for durable admission, closes the listener, and closes its store', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('offline')
  const gate = segmentSyncGate()
  const harness = await launchTraceHarness({ fs: gate.fs, gateway, groupCommitMs: 1, owner: owner('a'), userData })

  try {
    let postSettled = false

    const pendingPost = harness.post(payload('quit-boundary'), 1).then(response => {
      postSettled = true

      return response
    })

    await gate.waitUntilBlocked()
    const quitting = harness.quit()
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.equal(postSettled, false)
    gate.release()
    assert.equal((await pendingPost).status, 200)
    assert.equal((await quitting).pending, 1)

    await assert.rejects(harness.store.enqueue(envelopeFor(harness, payload('after-quit'), 2)), /trace_outbox_closed/)
    await assert.rejects(fetch(harness.endpoint), /fetch failed|ECONNREFUSED/)
  } finally {
    gate.release()
    await cleanupContinuityTest([harness], [gateway], userData)
  }
})

test('a late Gateway receipt after stop cannot mutate the closed real store journal', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('online')
  const held = gateway.holdNextResponse()
  const harness = await launchTraceHarness({ gateway, owner: owner('a'), userData })

  try {
    const post = harness.post(payload('late-receipt'), 1)
    await held.received
    assert.equal((await post).status, 200)
    await harness.quit()
    const journalPath = join(harness.root, 'index.journal')
    const stoppedJournal = await readFile(journalPath)

    held.release()
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.deepEqual(await readFile(journalPath), stoppedJournal)
    await assert.rejects(harness.store.enqueue(envelopeFor(harness, payload('closed-store'), 2)), /trace_outbox_closed/)
  } finally {
    held.release()
    await cleanupContinuityTest([harness], [gateway], userData)
  }
})

test('offline accepted Trace survives quit and uploads FIFO only from the same-account namespace', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('offline')
  const firstBody = payload('offline-one')
  const secondBody = payload('offline-two')
  const harnesses: TraceHarness[] = []

  try {
    const first = await launchTraceHarness({ gateway, owner: owner('a'), userData })
    harnesses.push(first)
    assert.equal((await first.post(firstBody, 1)).status, 200)
    assert.equal((await first.post(secondBody, 2)).status, 200)
    assert.equal((await first.quit()).pending, 2)

    const encrypted = await readFile(join(first.root, 'segments', 'active.segment'))
    assert.equal(encrypted.includes(firstBody), false)
    assert.equal(encrypted.includes(secondBody), false)

    const requestsBeforeWrongAccount = gateway.attempts.length
    gateway.setOnline(true)
    const wrong = await launchTraceHarness({ gateway, owner: owner('b'), userData })
    harnesses.push(wrong)
    await wrong.trigger()
    assert.equal((await wrong.diagnostics()).pending, 0)
    assert.equal(gateway.attempts.length, requestsBeforeWrongAccount)
    await wrong.quit()

    await assert.rejects(
      TraceOutboxStore.open({
        expectedOwner: owner('b'),
        groupCommitMs: 1,
        keyProtector: protector(),
        root: first.root
      }),
      /trace_outbox_account_mismatch/
    )

    const unavailable = await TraceOutboxStore.open({
      expectedOwner: owner('a'),
      groupCommitMs: 1,
      keyProtector: lostProtector(),
      root: first.root
    })

    assert.equal((await unavailable.diagnostics()).keyLost, 1)
    assert.equal((await unavailable.diagnostics()).quarantined, 2)
    assert.equal(await unavailable.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
    await unavailable.close()

    const resumed = await launchTraceHarness({ gateway, owner: owner('a'), userData })
    harnesses.push(resumed)
    await resumed.trigger()
    await waitFor(async () => (await resumed.diagnostics()).pending === 0)
    assert.deepEqual(
      [...gateway.logical.values()].map(batch => batch.digest),
      [sha256(firstBody), sha256(secondBody)]
    )
    assert.equal(gateway.logicalBatchCount, 2)
    assert.deepEqual(
      gateway.attempts.filter(attempt => attempt.outcome === 'accepted').map(attempt => attempt.outcome),
      ['accepted', 'accepted']
    )
    await resumed.quit()
  } finally {
    await cleanupContinuityTest(harnesses, [gateway], userData)
  }
})

test('a durable online Gateway receipt leaves only a bounded payload-free tombstone', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('online')
  const body = payload('gateway-first')
  const harnesses: TraceHarness[] = []

  try {
    const harness = await launchTraceHarness({
      gateway,
      groupCommitMs: 50,
      owner: owner('a'),
      receiptCapacityBytes: 2_048,
      userData
    })
    harnesses.push(harness)

    assert.equal((await harness.post(body, 1)).status, 200)
    await waitFor(async () => (await harness.diagnostics()).pending === 0)
    const diagnostics = await harness.diagnostics()

    assert.equal(diagnostics.payloadBytes, 0)
    assert.equal(diagnostics.tombstones, 1)
    assert.ok(diagnostics.tombstoneBytes > 0 && diagnostics.tombstoneBytes <= 2_048)
    assert.equal(gateway.logicalBatchCount, 1)
    assert.deepEqual(
      [...gateway.logical.values()].map(batch => batch.digest),
      [sha256(body)]
    )
    await harness.quit()

    const reopened = await TraceOutboxStore.open({
      expectedOwner: owner('a'),
      groupCommitMs: 1,
      keyProtector: protector(),
      root: harness.root
    })
    assert.equal((await reopened.diagnostics()).payloadBytes, 0)
    assert.equal((await reopened.diagnostics()).tombstones, 1)
    await reopened.close()
  } finally {
    await cleanupContinuityTest(harnesses, [gateway], userData)
  }
})

test('a lost durable Gateway response reuses the same batch id and digest after restart-safe local commit', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('online')
  gateway.loseNextResponse()
  const body = payload('lost-response')
  const harnesses: TraceHarness[] = []

  try {
    const harness = await launchTraceHarness({ gateway, owner: owner('a'), userData })
    harnesses.push(harness)
    assert.equal((await harness.post(body, 1)).status, 200)
    await waitFor(async () => gateway.attempts.length >= 2 && (await harness.diagnostics()).pending === 0)

    assert.equal(gateway.logicalBatchCount, 1)
    assert.equal(gateway.attempts[0].outcome, 'lost')
    assert.equal(gateway.attempts[1].outcome, 'duplicate')
    assert.equal(gateway.attempts[0].batchId, gateway.attempts[1].batchId)
    assert.equal(gateway.attempts[0].digest, gateway.attempts[1].digest)
    assert.deepEqual(gateway.attempts[0].body, gateway.attempts[1].body)
    assert.equal((await harness.diagnostics()).payloadBytes, 0)
    await harness.quit()
  } finally {
    await cleanupContinuityTest(harnesses, [gateway], userData)
  }
})

async function postLoopback(endpoint: string, bearer: string, body: Buffer, sequence: number) {
  return fetch(endpoint, {
    method: 'POST',
    body: body as unknown as BodyInit,
    headers: {
      authorization: `Bearer ${bearer}`,
      'content-type': 'application/x-protobuf',
      'x-hermes-session-id': `session-${sequence}`,
      'x-telemetry-schema-version': '1',
      'x-trace-entrypoint': 'desktop',
      'x-trace-run-id': `run-${sequence}`
    }
  })
}

function singleHeader(value: string | string[] | undefined): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

async function waitFor(predicate: () => boolean | Promise<boolean>): Promise<void> {
  const deadline = Date.now() + 5_000

  while (!(await predicate())) {
    if (Date.now() >= deadline) {
      assert.fail('timed out waiting for trace continuity')
    }

    await new Promise(resolve => setTimeout(resolve, 5))
  }
}
