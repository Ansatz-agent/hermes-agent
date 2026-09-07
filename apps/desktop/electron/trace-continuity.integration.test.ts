import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import http from 'node:http'
import { join } from 'node:path'

import { test } from 'vitest'

import { AuthBridgeError, type TraceCredential } from './auth-bridge'
import { RebindableTraceCredentialSource } from './trace-credential-provider'
import {
  type TraceDurabilityDiagnostic,
  TraceDurabilityRuntime,
  type TraceDurabilitySession
} from './trace-durability-runtime'
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

function traceCredential(): TraceCredential {
  const expiresAt = Date.now() + 10 * 60_000

  return {
    access_token: 'integration-trace-token-abcdefghijklmnopqrstuvwxyz',
    expires_at: new Date(expiresAt).toISOString(),
    expires_in: 600,
    installation_id: installationId
  }
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
  private heldAllResponses: { received: () => void; release: Promise<void> } | null = null
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

  holdAllResponses(): { received: Promise<void>; release(): void } {
    let markReceived!: () => void
    let releaseGate!: () => void

    const received = new Promise<void>(resolve => {
      markReceived = resolve
    })

    const released = new Promise<void>(resolve => {
      releaseGate = resolve
    })

    const gate = { received: markReceived, release: released }

    this.heldAllResponses = gate

    return {
      received,
      release: () => {
        if (this.heldAllResponses === gate) {
          this.heldAllResponses = null
        }

        releaseGate()
      }
    }
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

    const heldAllResponses = this.heldAllResponses

    if (heldAllResponses !== null) {
      heldAllResponses.received()
      await heldAllResponses.release
    }

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
  credentialLoader?: (owner: TraceOwner, forceRefresh: boolean) => Promise<TraceCredential>
  fetchImpl?: (input: string | URL, init?: RequestInit) => Promise<Response>
  fs?: TraceFileSystem
  gateway: ControllableGateway
  groupCommitMs?: number
  keyProtector?: TraceKeyProtector
  localCommitError?: Error
  owner: TraceOwner
  random?: () => number
  receiptCapacityBytes?: number
  runtime?: TraceDurabilityRuntime
  userData: string
}) {
  const root = join(options.userData, 'trace-outbox', options.owner.accountKey)
  let currentOwner = { ...options.owner }
  const admittedOwners: TraceOwner[] = []

  const store = await TraceOutboxStore.open({
    expectedOwner: options.owner,
    fs: options.fs,
    groupCommitMs: options.groupCommitMs ?? 1,
    keyProtector: options.keyProtector ?? protector(),
    receiptCapacityBytes: options.receiptCapacityBytes,
    root
  })

  const credentialLoader = options.credentialLoader ?? (async () => traceCredential())
  const credentialSource = new RebindableTraceCredentialSource()
  const diagnosticScope = options.runtime?.bindDiagnostics()

  const bindCredential = (nextOwner: TraceOwner) => {
    credentialSource.bind(nextOwner, forceRefresh => credentialLoader(nextOwner, forceRefresh))
  }

  bindCredential(currentOwner)
  const credentialProvider = new RefreshingTraceCredentialProvider(credentialSource, { installationId })

  const forwarderStore = {
    acknowledge: store.acknowledge.bind(store),
    beginEnqueue(input: TraceEnvelopeInput) {
      admittedOwners.push({ ...input.owner })

      if (options.localCommitError) {
        return {
          batchId: sha256(Buffer.concat([input.body, Buffer.from(input.runId, 'utf8')])),
          cancelForGatewayReceipt: async () => {
            throw options.localCommitError
          },
          durable: Promise.reject(options.localCommitError)
        }
      }

      return store.beginEnqueue(input)
    },
    close: () => store.close(),
    diagnostics: () => store.diagnostics(),
    peekEligible: (now: number) => store.peekEligible(now),
    quarantine: (batchId: string, errorClass: string) => store.quarantine(batchId, errorClass),
    quarantineInput: (input: TraceEnvelopeInput, errorClass: string) => store.quarantineInput(input, errorClass)
  }

  const observedStore = diagnosticScope?.observeStore(forwarderStore) ?? forwarderStore

  let forwarder!: TraceForwarder
  let lifecycle!: TraceRecoveryLifecycle

  const controller = new TraceRecoveryController({
    accountKey: options.owner.accountKey,
    pump: async () => {
      await forwarder.pump()
      const nextRecoveryAt = forwarder.nextRecoveryAt()

      diagnosticScope?.observeRecovery(await store.diagnostics(), nextRecoveryAt)
      lifecycle.scheduleRetryAt(nextRecoveryAt)
    }
  })

  forwarder = new TraceForwarder({
    credentialProvider,
    fetchImpl: options.fetchImpl ?? fetch,
    installationId,
    random: options.random,
    recovery: controller,
    store: observedStore,
    upstreamUrl: options.gateway.endpoint
  })
  const started = await forwarder.start(options.owner)
  lifecycle = new TraceRecoveryLifecycle({ controller, credentialProvider, periodicMs: 60 * 60_000 })
  lifecycle.start()

  return {
    admittedOwners,
    endpoint: started.endpoint,
    localBearer: started.localBearer,
    get owner() {
      return { ...currentOwner }
    },
    root,
    store,
    async diagnostics() {
      return store.diagnostics()
    },
    ingress() {
      return forwarder.ingress()
    },
    async post(body: Buffer, sequence: number) {
      return postLoopback(started.endpoint, started.localBearer, body, sequence)
    },
    rebind(nextOwner: TraceOwner) {
      bindCredential(nextOwner)
      forwarder.rebindOwner(nextOwner)
      currentOwner = { ...nextOwner }
    },
    async quit() {
      credentialSource.clear()
      const [, summary] = await Promise.all([lifecycle.stop(), forwarder.stop({ flushMs: 3_000 })])

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
  return envelopeForOwner(harness.owner, body, sequence)
}

function envelopeForOwner(owner: TraceOwner, body: Buffer, sequence: number): TraceEnvelopeInput {
  return {
    body,
    contentType: 'application/x-protobuf',
    entrypoint: 'desktop',
    hermesSessionId: `session-${sequence}`,
    owner,
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

function armableSegmentSyncGate(): {
  arm(): void
  fs: TraceFileSystem
  release(): void
  waitUntilBlocked(): Promise<void>
} {
  let armed = false
  let consumed = false
  let markBlocked!: () => void
  let release!: () => void
  let released = false

  const blocked = new Promise<void>(resolve => {
    markBlocked = resolve
  })

  const releasePromise = new Promise<void>(resolve => {
    release = () => {
      released = true
      resolve()
    }
  })

  return {
    arm: () => {
      armed = true
    },
    fs: {
      ...nodeTraceFileSystem,
      async syncFile(path: string) {
        if (armed && !consumed && isActiveSegmentPath(path) && !released) {
          consumed = true
          markBlocked()
          await releasePromise
        }

        await nodeTraceFileSystem.syncFile(path)
      }
    },
    release,
    waitUntilBlocked: () => blocked
  }
}

async function readPersistedOwners(
  root: string,
  expectedOwner: TraceOwner,
  expectedCount: number
): Promise<TraceOwner[]> {
  const store = await TraceOutboxStore.open({
    expectedOwner,
    groupCommitMs: 1,
    keyProtector: protector(),
    root
  })

  const owners: TraceOwner[] = []

  try {
    for (let index = 0; index < expectedCount; index += 1) {
      const batch = await store.peekEligible(Number.MAX_SAFE_INTEGER)

      if (batch === undefined) {
        return owners
      }

      owners.push({ ...batch.owner })
      await store.acknowledge(batch.batchId, {
        batchId: batch.batchId,
        outcome: 'accepted',
        receivedAt: Date.now()
      })
    }

    assert.equal(await store.peekEligible(Number.MAX_SAFE_INTEGER), undefined, 'persisted owner audit exceeded limit')

    return owners
  } finally {
    await store.close()
  }
}

function failingCommitFileSystem(kind: 'disk-full' | 'journal-integrity'): TraceFileSystem {
  return {
    ...nodeTraceFileSystem,
    appendFile: async (path, data) => {
      if (kind === 'disk-full' && isActiveSegmentPath(path)) {
        throw Object.assign(new Error('Bearer secret access_token wrappedKey payload'), { code: 'ENOSPC' })
      }

      await nodeTraceFileSystem.appendFile(path, data)
    },
    syncFile: async path => {
      if (kind === 'journal-integrity' && path.endsWith('index.journal')) {
        throw Object.assign(new Error('invalid_journal_checksum Bearer secret payload'), { code: 'EIO' })
      }

      await nodeTraceFileSystem.syncFile(path)
    }
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

test('same running Trace session resumes an offline durable batch after the Gateway returns online', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('offline')
  const body = payload('in-process-resume')
  const harnesses: TraceHarness[] = []

  try {
    const harness = await launchTraceHarness({ gateway, owner: owner('a'), random: () => 0.5, userData })
    harnesses.push(harness)

    assert.equal((await harness.post(body, 1)).status, 200)
    await waitFor(async () => {
      const diagnostics = await harness.diagnostics()
      const unavailableAttempts = gateway.attempts.filter(attempt => attempt.outcome === 'unavailable')

      return diagnostics.pending === 1 && unavailableAttempts.length >= 2
    })

    const unavailable = gateway.attempts.filter(attempt => attempt.outcome === 'unavailable').slice(-1)[0]
    assert.ok(unavailable)
    assert.equal(unavailable.digest, sha256(body))

    gateway.setOnline(true)
    await waitFor(async () => (await harness.diagnostics()).pending === 0)

    const receipts = gateway.attempts.filter(
      attempt => attempt.outcome === 'accepted' || attempt.outcome === 'duplicate'
    )

    assert.equal(gateway.logicalBatchCount, 1)
    assert.equal(receipts.length, 1)
    assert.equal(receipts[0]?.batchId, unavailable.batchId)
    assert.deepEqual([...gateway.logical.values()], [{ body, digest: sha256(body) }])
  } finally {
    await cleanupContinuityTest(harnesses, [gateway], userData)
  }
})

test('auth bridge and Trace service outages keep 25 admissions durable and upload them after restart', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('online')
  const harnesses: TraceHarness[] = []
  const bodies = Array.from({ length: 25 }, (_, index) => payload(`auth-outage-${index}`))
  let credentialCalls = 0
  let upstreamCalls = 0

  try {
    const unavailable = await launchTraceHarness({
      credentialLoader: async () => {
        credentialCalls += 1
        throw new AuthBridgeError('runtime_unavailable', 'runtime_unavailable')
      },
      fetchImpl: async () => {
        upstreamCalls += 1
        throw new Error('trace-upstream-offline')
      },
      gateway,
      owner: owner('a'),
      userData
    })

    harnesses.push(unavailable)

    for (const [index, body] of bodies.entries()) {
      assert.equal((await unavailable.post(body, index)).status, 200)
    }

    assert.equal((await unavailable.diagnostics()).pending, 25)
    assert.ok(credentialCalls > 0)
    assert.equal(upstreamCalls, 0)
    assert.equal((await unavailable.quit()).pending, 25)

    gateway.loseNextResponse()
    const resumed = await launchTraceHarness({ gateway, owner: owner('a'), userData })
    harnesses.push(resumed)
    await resumed.trigger()
    await waitFor(async () => (await resumed.diagnostics()).pending === 0)

    assert.equal(new Set(gateway.attempts.map(attempt => attempt.batchId)).size, 25)

    const receipts = gateway.attempts.filter(
      attempt => attempt.outcome === 'accepted' || attempt.outcome === 'duplicate'
    )

    assert.equal(receipts.length, 25)
    assert.equal(gateway.attempts.filter(attempt => attempt.outcome === 'lost').length, 1)
    assert.equal(gateway.attempts.filter(attempt => attempt.outcome === 'duplicate').length, 1)
    assert.equal(
      gateway.attempts.find(attempt => attempt.outcome === 'lost')?.batchId,
      gateway.attempts.find(attempt => attempt.outcome === 'duplicate')?.batchId
    )
    assert.equal(gateway.logicalBatchCount, 25)
    assert.deepEqual(
      [...gateway.logical.values()].map(batch => batch.digest),
      bodies.map(sha256)
    )
    await resumed.quit()
  } finally {
    await cleanupContinuityTest(harnesses, [gateway], userData)
  }
})

test('same-account Session rebind keeps ingress stable and recovers old and new owner batches together', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('online')
  const initialOwner = owner('a')

  const reboundOwner = {
    ...initialOwner,
    sessionId: 'aaaaaaaa-3333-4333-8333-aaaaaaaaaaaa'
  }

  const activeScope = {
    connection_id: 'local',
    epoch: 11,
    runtime_instance_id: 'runtime-continuity'
  }

  const harnesses: TraceHarness[] = []
  const gate = armableSegmentSyncGate()

  const bodies = Array.from({ length: 25 }, (_, index) =>
    payload(index < 13 ? `before-rebind-${index}` : `after-rebind-${index}`)
  )

  try {
    const harness = await launchTraceHarness({
      credentialLoader: async () => {
        throw new AuthBridgeError('runtime_unavailable', 'runtime_unavailable')
      },
      fs: gate.fs,
      gateway,
      owner: initialOwner,
      userData
    })

    harnesses.push(harness)
    const stableIngress = { endpoint: harness.endpoint, localBearer: harness.localBearer }
    let currentOwner = { ...initialOwner }
    let currentScope = { ...activeScope }

    const session: TraceDurabilitySession = {
      compactIfIdle: async () => false,
      context: () => ({ ingress: stableIngress, owner: currentOwner, scope: currentScope }),
      rebind(nextOwner, nextScope) {
        harness.rebind(nextOwner)
        currentOwner = { ...nextOwner }
        currentScope = { ...nextScope }
      },
      stop: async () => {},
      trigger: () => void harness.trigger()
    }

    const runtime = new TraceDurabilityRuntime()
    await runtime.activate({ owner: initialOwner, scope: activeScope }, async () => session)

    for (let index = 0; index < 12; index += 1) {
      assert.equal((await harness.post(bodies[index], index)).status, 200)
    }

    gate.arm()
    const inFlightAdmission = harness.post(bodies[12], 12)
    await gate.waitUntilBlocked()
    const rebound = await runtime.activate({ owner: reboundOwner, scope: activeScope }, async () => session)
    assert.equal(rebound.kind, 'rebound')
    assert.deepEqual(harness.ingress(), stableIngress)
    gate.release()
    assert.equal((await inFlightAdmission).status, 200)

    for (let index = 13; index < 25; index += 1) {
      assert.equal((await harness.post(bodies[index], index)).status, 200)
    }

    assert.equal(harness.admittedOwners.length, 25)
    assert.equal(harness.admittedOwners.filter(value => value.sessionId === initialOwner.sessionId).length, 13)
    assert.equal(harness.admittedOwners.filter(value => value.sessionId === reboundOwner.sessionId).length, 12)
    assert.equal(
      harness.admittedOwners.every(value => value.accountKey === initialOwner.accountKey),
      true
    )
    assert.equal((await harness.diagnostics()).pending, 25)
    assert.equal((await harness.quit()).pending, 25)

    const auditRoot = join(userData, 'persisted-owner-audit')
    await cp(harness.root, auditRoot, { recursive: true })
    const persistedOwners = await readPersistedOwners(auditRoot, reboundOwner, 25)

    assert.equal(persistedOwners.length, 25)
    assert.equal(persistedOwners.filter(value => value.sessionId === initialOwner.sessionId).length, 13)
    assert.equal(persistedOwners.filter(value => value.sessionId === reboundOwner.sessionId).length, 12)
    assert.equal(
      persistedOwners.every(value => value.accountKey === initialOwner.accountKey),
      true
    )

    let credentialsAvailable = false
    let resumedCredentialCalls = 0

    const resumed = await launchTraceHarness({
      credentialLoader: async () => {
        resumedCredentialCalls += 1

        if (!credentialsAvailable) {
          throw new AuthBridgeError('runtime_unavailable', 'runtime_unavailable')
        }

        return traceCredential()
      },
      gateway,
      owner: reboundOwner,
      userData
    })

    harnesses.push(resumed)
    await waitFor(() => resumedCredentialCalls > 0)
    assert.equal((await resumed.diagnostics()).pending, 25)
    credentialsAvailable = true
    await resumed.trigger()
    await waitFor(async () => (await resumed.diagnostics()).pending === 0)
    assert.equal(gateway.logicalBatchCount, 25)
    assert.deepEqual(
      [...gateway.logical.values()].map(batch => batch.digest),
      bodies.map(sha256)
    )
    assert.equal(
      gateway.attempts.every(attempt => attempt.outcome === 'accepted' || attempt.outcome === 'duplicate'),
      true
    )
    await resumed.quit()
  } finally {
    gate.release()
    await cleanupContinuityTest(harnesses, [gateway], userData)
  }
})

test('local durability failures return 503 and emit one secret-free storage transition', async () => {
  const cases: readonly {
    errorClass: string
    fs?: TraceFileSystem
    label: string
    localCommitError?: Error
  }[] = [
    {
      errorClass: 'disk_full',
      fs: failingCommitFileSystem('disk-full'),
      label: 'enospc'
    },
    {
      errorClass: 'secure_key_storage_unavailable',
      label: 'secure-storage',
      localCommitError: Object.assign(new Error('secure_key_storage_unavailable'), {
        secret: 'Bearer secret access_token wrappedKey payload'
      })
    },
    {
      errorClass: 'journal_integrity_failure',
      fs: failingCommitFileSystem('journal-integrity'),
      label: 'journal-integrity'
    }
  ]

  for (const testCase of cases) {
    const userData = await temporaryUserData()
    const gateway = await ControllableGateway.start('online')
    const events: TraceDurabilityDiagnostic[] = []
    const runtime = new TraceDurabilityRuntime(event => events.push(event))
    const harnesses: TraceHarness[] = []
    let upstreamCalls = 0

    try {
      const harness = await launchTraceHarness({
        fetchImpl: async () => {
          upstreamCalls += 1
          throw new Error('trace-upstream-offline')
        },
        fs: testCase.fs,
        gateway,
        localCommitError: testCase.localCommitError,
        owner: owner('a'),
        runtime,
        userData
      })

      harnesses.push(harness)
      assert.equal((await harness.post(payload(`storage-${testCase.label}`), 1)).status, 503)
      assert.equal(upstreamCalls, 1)
      assert.deepEqual(
        events.map(event => event.code),
        ['trace_storage_failed']
      )
      assert.equal(events[0]?.errorClass, testCase.errorClass)
      assert.doesNotMatch(JSON.stringify(events), /Bearer |access_token|wrappedKey|payload/)
    } finally {
      await cleanupContinuityTest(harnesses, [gateway], userData)
    }
  }
})

test('an unavailable real key protector is classified as a local storage failure before publication', async () => {
  const userData = await temporaryUserData()
  const events: TraceDurabilityDiagnostic[] = []
  const runtime = new TraceDurabilityRuntime(event => events.push(event))
  const diagnostics = runtime.bindDiagnostics()

  try {
    await assert.rejects(
      (async () => {
        try {
          await TraceOutboxStore.open({
            expectedOwner: owner('a'),
            keyProtector: {
              available: () => false,
              unwrap: () => {
                throw new Error('must_not_unwrap')
              },
              wrap: () => {
                throw new Error('must_not_wrap')
              }
            },
            root: join(userData, 'trace-outbox', owner('a').accountKey)
          })
        } catch (error) {
          diagnostics.storageFailed(error)
          throw error
        }
      })(),
      /secure_key_storage_unavailable/
    )

    assert.deepEqual(events, [{ code: 'trace_storage_failed', errorClass: 'secure_key_storage_unavailable' }])
  } finally {
    await rm(userData, { force: true, recursive: true })
  }
})

test('hard lock isolates account B from account A storage and a delayed account A receipt', async () => {
  const userData = await temporaryUserData()
  const gatewayA = await ControllableGateway.start('online')
  const gatewayB = await ControllableGateway.start('online')
  const heldA = gatewayA.holdAllResponses()
  const ownerA = owner('a')
  const ownerB = owner('b')

  const activeScope = {
    connection_id: 'local',
    epoch: 21,
    runtime_instance_id: 'runtime-isolation'
  }

  const harnesses: TraceHarness[] = []
  const runtime = new TraceDurabilityRuntime()

  try {
    const harnessA = await launchTraceHarness({ gateway: gatewayA, owner: ownerA, runtime, userData })
    harnesses.push(harnessA)

    const sessionA: TraceDurabilitySession = {
      compactIfIdle: async () => false,
      context: () => ({
        ingress: { endpoint: harnessA.endpoint, localBearer: harnessA.localBearer },
        owner: ownerA,
        scope: activeScope
      }),
      rebind: nextOwner => harnessA.rebind(nextOwner),
      stop: async () => void (await harnessA.quit()),
      trigger: () => void harnessA.trigger()
    }

    await runtime.activate({ owner: ownerA, scope: activeScope }, async () => sessionA)
    assert.equal((await harnessA.post(payload('account-a-delayed'), 1)).status, 200)
    await heldA.received
    const accountABatch = await harnessA.store.peekEligible(Number.MAX_SAFE_INTEGER)

    assert.ok(accountABatch)
    assert.equal((await harnessA.diagnostics()).pending, 1)
    await runtime.lock(0)

    const harnessB = await launchTraceHarness({ gateway: gatewayB, owner: ownerB, runtime, userData })
    harnesses.push(harnessB)

    const sessionB: TraceDurabilitySession = {
      compactIfIdle: async () => false,
      context: () => ({
        ingress: { endpoint: harnessB.endpoint, localBearer: harnessB.localBearer },
        owner: ownerB,
        scope: activeScope
      }),
      rebind: nextOwner => harnessB.rebind(nextOwner),
      stop: async () => void (await harnessB.quit()),
      trigger: () => void harnessB.trigger()
    }

    await runtime.activate({ owner: ownerB, scope: activeScope }, async () => sessionB)
    assert.equal((await harnessB.post(payload('account-b-accepted'), 2)).status, 200)
    await waitFor(async () => (await harnessB.diagnostics()).pending === 0)

    const beforeDelayedReceipt = await harnessB.diagnostics()
    const accountBKey = await readFile(join(harnessB.root, 'key.json'), 'utf8')
    const accountBBatchId = gatewayB.attempts[0]?.batchId

    assert.notEqual(harnessA.root, harnessB.root)
    assert.equal(
      harnessB.admittedOwners.every(value => value.accountKey === ownerB.accountKey),
      true
    )
    assert.equal(accountBKey.includes(ownerA.accountKey), false)
    assert.equal(accountBKey.includes(ownerA.sessionId!), false)
    assert.notEqual(accountBBatchId, accountABatch.batchId)
    assert.equal(await harnessB.store.lookupReceipt(accountABatch.batchId), undefined)

    heldA.release()
    await waitFor(() => gatewayA.attempts.length > 0)
    await new Promise(resolve => setTimeout(resolve, 20))

    assert.deepEqual(await harnessB.diagnostics(), beforeDelayedReceipt)
    assert.equal(await harnessB.store.lookupReceipt(accountABatch.batchId), undefined)
    assert.deepEqual(runtime.current()?.owner, ownerB)
  } finally {
    heldA.release()
    await cleanupContinuityTest(harnesses, [gatewayA, gatewayB], userData)
  }
})

test('native login migrates a same-principal local-only FIFO through a synced staging namespace without changing batch ids', async () => {
  const userData = await temporaryUserData()

  const sourceOwner: TraceOwner = {
    accountId: null,
    accountKey: `legacy-${'c'.repeat(64)}`,
    installationId,
    sessionId: null
  }

  const targetOwner = owner('a')
  const sourceRoot = join(userData, 'trace-outbox', sourceOwner.accountKey)
  const targetRoot = join(userData, 'trace-outbox', targetOwner.accountKey)

  try {
    const source = await TraceOutboxStore.open({
      expectedOwner: sourceOwner,
      keyProtector: protector(),
      root: sourceRoot
    })

    const first = await source.enqueue(envelopeForOwner(sourceOwner, payload('migrate-one'), 1))
    const second = await source.enqueue(envelopeForOwner(sourceOwner, payload('migrate-two'), 2))
    await source.close()

    assert.equal(
      await TraceOutboxStore.migrateTrustedNamespace({
        keyProtector: protector(),
        removeSourceDirectory: path => rm(path, { force: false, recursive: true }),
        sourceOwner,
        sourceRoot,
        targetOwner,
        targetRoot
      }),
      true
    )
    await assert.rejects(readFile(join(sourceRoot, 'key.json')), /ENOENT/)

    const migrated = await TraceOutboxStore.open({
      expectedOwner: targetOwner,
      keyProtector: protector(),
      root: targetRoot
    })

    const migratedFirst = await migrated.peekEligible(Number.MAX_SAFE_INTEGER)
    assert.equal(migratedFirst?.batchId, first.batchId)
    assert.deepEqual(migratedFirst?.owner, targetOwner)
    await migrated.acknowledge(first.batchId, {
      batchId: first.batchId,
      outcome: 'accepted',
      receivedAt: Date.now()
    })
    assert.equal((await migrated.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, second.batchId)
    await migrated.close()

    assert.equal(
      await TraceOutboxStore.migrateTrustedNamespace({
        keyProtector: protector(),
        removeSourceDirectory: path => rm(path, { force: false, recursive: true }),
        sourceOwner,
        sourceRoot,
        targetOwner,
        targetRoot
      }),
      false
    )
  } finally {
    await rm(userData, { force: true, recursive: true })
  }
})

test('trusted migration fails closed and preserves the source when the target key is lost on disk', async () => {
  const userData = await temporaryUserData()

  const sourceOwner: TraceOwner = {
    accountId: null,
    accountKey: `legacy-${'d'.repeat(64)}`,
    installationId,
    sessionId: null
  }

  const targetOwner = owner('a')
  const sourceRoot = join(userData, 'trace-outbox', sourceOwner.accountKey)
  const targetRoot = join(userData, 'trace-outbox', targetOwner.accountKey)

  try {
    const source = await TraceOutboxStore.open({
      expectedOwner: sourceOwner,
      keyProtector: protector(),
      root: sourceRoot
    })

    const first = await source.enqueue(envelopeForOwner(sourceOwner, payload('key-loss-one'), 1))
    const second = await source.enqueue(envelopeForOwner(sourceOwner, payload('key-loss-two'), 2))
    await source.close()

    const target = await TraceOutboxStore.open({
      expectedOwner: targetOwner,
      keyProtector: protector(),
      root: targetRoot
    })

    await target.close()

    // Simulate safeStorage losing the ability to unwrap only the persisted
    // target key: the record still parses, but decryption fails.
    const targetKeyPath = join(targetRoot, 'key.json')
    const persisted = JSON.parse(await readFile(targetKeyPath, 'utf8')) as Record<string, unknown>
    persisted.wrappedKey = Buffer.from('no-longer-decryptable').toString('base64')
    await writeFile(targetKeyPath, JSON.stringify(persisted), { mode: 0o600 })

    await assert.rejects(
      TraceOutboxStore.migrateTrustedNamespace({
        keyProtector: protector(),
        removeSourceDirectory: path => rm(path, { force: false, recursive: true }),
        sourceOwner,
        sourceRoot,
        targetOwner,
        targetRoot
      }),
      /trace_namespace_migration_target_unavailable/
    )

    // The source namespace must survive intact: key present, FIFO preserved.
    await readFile(join(sourceRoot, 'key.json'))

    const reopened = await TraceOutboxStore.open({
      expectedOwner: sourceOwner,
      keyProtector: protector(),
      root: sourceRoot
    })

    assert.equal((await reopened.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, first.batchId)
    await reopened.acknowledge(first.batchId, { batchId: first.batchId, outcome: 'accepted', receivedAt: Date.now() })
    assert.equal((await reopened.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, second.batchId)
    assert.equal((await reopened.diagnostics()).keyLost, 0)
    await reopened.close()
  } finally {
    await rm(userData, { force: true, recursive: true })
  }
})

test('trusted migration streams records, merges an existing same-account target, preserves receipts, and drains source only after success', async () => {
  const userData = await temporaryUserData()

  const sourceOwner: TraceOwner = {
    accountId: null,
    accountKey: `legacy-${'e'.repeat(64)}`,
    installationId,
    sessionId: null
  }

  const targetOwner = owner('a')
  const sourceRoot = join(userData, 'trace-outbox', sourceOwner.accountKey)
  const targetRoot = join(userData, 'trace-outbox', targetOwner.accountKey)
  let readsInFlight = 0
  let maximumReadsInFlight = 0

  const streamingFs: TraceFileSystem = {
    ...nodeTraceFileSystem,
    readRange: async (path, offset, length) => {
      readsInFlight += 1
      maximumReadsInFlight = Math.max(maximumReadsInFlight, readsInFlight)

      try {
        await Promise.resolve()

        return await nodeTraceFileSystem.readRange(path, offset, length)
      } finally {
        readsInFlight -= 1
      }
    }
  }

  try {
    const source = await TraceOutboxStore.open({
      expectedOwner: sourceOwner,
      keyProtector: protector(),
      root: sourceRoot
    })

    const accepted = await source.enqueue(envelopeForOwner(sourceOwner, payload('migrated-receipt'), 1))
    await source.acknowledge(accepted.batchId, {
      batchId: accepted.batchId,
      outcome: 'accepted',
      receivedAt: Date.now()
    })
    const pending = await source.enqueue(envelopeForOwner(sourceOwner, payload('migrated-pending'), 2))
    await source.close()

    const target = await TraceOutboxStore.open({
      expectedOwner: targetOwner,
      keyProtector: protector(),
      root: targetRoot
    })

    await target.enqueue(envelopeForOwner(targetOwner, payload('already-target'), 0))
    await target.close()

    assert.equal(
      await TraceOutboxStore.migrateTrustedNamespace({
        fs: streamingFs,
        keyProtector: protector(),
        removeSourceDirectory: path => rm(path, { force: false, recursive: true }),
        sourceOwner,
        sourceRoot,
        targetOwner,
        targetRoot
      }),
      true
    )
    assert.equal(maximumReadsInFlight, 1)
    await assert.rejects(readFile(join(sourceRoot, 'key.json')), /ENOENT/)

    const merged = await TraceOutboxStore.open({
      expectedOwner: targetOwner,
      keyProtector: protector(),
      root: targetRoot
    })

    const diagnostics = await merged.diagnostics()
    assert.equal(diagnostics.tombstones, 1)
    assert.equal((await merged.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, pending.batchId)
    const ids: string[] = []

    for (;;) {
      const batch = await merged.peekEligible(Number.MAX_SAFE_INTEGER)

      if (!batch) {
        break
      }

      ids.push(batch.batchId)
      await merged.acknowledge(batch.batchId, { batchId: batch.batchId, outcome: 'accepted', receivedAt: Date.now() })
    }

    assert.ok(ids.includes(pending.batchId))
    assert.equal(ids[0], pending.batchId)
    await merged.close()
  } finally {
    await rm(userData, { force: true, recursive: true })
  }
})

test('receipt-only migration recomputes target dedupe after source payload compaction', async () => {
  const userData = await temporaryUserData()

  const sourceOwner: TraceOwner = {
    accountId: null,
    accountKey: `legacy-${'f'.repeat(64)}`,
    installationId,
    sessionId: null
  }

  const targetOwner = owner('a')
  const sourceRoot = join(userData, 'trace-outbox', sourceOwner.accountKey)
  const targetRoot = join(userData, 'trace-outbox', targetOwner.accountKey)
  const sourceEnvelope = envelopeForOwner(sourceOwner, payload('receipt-only'), 9)

  try {
    const source = await TraceOutboxStore.open({
      expectedOwner: sourceOwner,
      keyProtector: protector(),
      root: sourceRoot
    })

    const accepted = await source.enqueue(sourceEnvelope)
    await source.acknowledge(accepted.batchId, {
      batchId: accepted.batchId,
      outcome: 'accepted',
      receivedAt: Date.now()
    })
    await source.compactIfIdle()
    assert.equal((await source.diagnostics()).payloadBytes, 0)
    await source.close()
    await assert.rejects(readFile(join(sourceRoot, 'segments', 'active.segment')), /ENOENT/)

    assert.equal(
      await TraceOutboxStore.migrateTrustedNamespace({
        keyProtector: protector(),
        removeSourceDirectory: path => rm(path, { force: false, recursive: true }),
        sourceOwner,
        sourceRoot,
        targetOwner,
        targetRoot
      }),
      true
    )

    const target = await TraceOutboxStore.open({
      expectedOwner: targetOwner,
      keyProtector: protector(),
      root: targetRoot
    })

    const duplicate = await target.enqueue({ ...sourceEnvelope, owner: targetOwner })
    assert.equal(duplicate.batchId, accepted.batchId)
    assert.equal((await target.diagnostics()).tombstones, 1)
    await target.close()
  } finally {
    await rm(userData, { force: true, recursive: true })
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

test('gateway-first cancellation leaves storage diagnostics available for a later real failure', async () => {
  const userData = await temporaryUserData()
  const gateway = await ControllableGateway.start('online')
  const events: TraceDurabilityDiagnostic[] = []
  const runtime = new TraceDurabilityRuntime(event => events.push(event))
  const harnesses: TraceHarness[] = []
  let acceptDirectUpload = true
  let failLocalCommit = false

  const fs: TraceFileSystem = {
    ...nodeTraceFileSystem,
    appendFile: async (path, data) => {
      if (failLocalCommit && isActiveSegmentPath(path)) {
        throw Object.assign(new Error('disk full after gateway-first cancellation'), { code: 'ENOSPC' })
      }

      await nodeTraceFileSystem.appendFile(path, data)
    }
  }

  try {
    const harness = await launchTraceHarness({
      fetchImpl: async (_input, init) => {
        if (!acceptDirectUpload) {
          throw new Error('trace-upstream-offline')
        }

        const batchId = new Headers(init?.headers).get('idempotency-key')
        assert.ok(batchId)

        return new Response(null, {
          headers: {
            'x-trace-batch-id': batchId,
            'x-trace-receipt': 'accepted'
          },
          status: 202
        })
      },
      fs,
      gateway,
      groupCommitMs: 50,
      owner: owner('a'),
      runtime,
      userData
    })

    harnesses.push(harness)
    assert.equal((await harness.post(payload('gateway-first-no-storage-failure'), 1)).status, 200)
    await waitFor(async () => (await harness.diagnostics()).pending === 0)
    assert.deepEqual(events, [])

    acceptDirectUpload = false
    failLocalCommit = true
    assert.equal((await harness.post(payload('real-storage-failure'), 2)).status, 503)
    assert.deepEqual(events, [{ code: 'trace_storage_failed', errorClass: 'disk_full', pending: 0, pendingBytes: 0 }])
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
