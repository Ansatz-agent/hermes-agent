import assert from 'node:assert/strict'
import { appendFile, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'

import { test } from 'vitest'

import { createSafeStorageTraceKeyProtector } from './trace-outbox-crypto'
import {
  nodeTraceFileSystem,
  type TraceFileSystem,
  TraceJournal,
  traceJournalOperationBytes
} from './trace-outbox-journal'
import { decodeSegmentRecord } from './trace-outbox-record'
import { TraceOutboxStore } from './trace-outbox-store'
import { type DurableReceipt, type TraceEnvelopeInput } from './trace-outbox-types'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void

  const promise = new Promise<T>(currentResolve => {
    resolve = currentResolve
  })

  return { promise, resolve }
}

class FakeTraceFileSystem implements TraceFileSystem {
  readonly allEvents: string[] = []
  readonly events: string[] = []
  readonly files = new Map<string, Buffer>()
  private readonly waiters = new Map<string, Array<() => void>>()

  constructor(
    private readonly hooks: Partial<
      Record<'segment.write' | 'segment.sync' | 'journal.write' | 'journal.sync', () => void | Promise<void>>
    > = {}
  ) {}

  async freeSpace(): Promise<{ available: number; total: number }> {
    return { available: 20 * 1024 ** 3, total: 20 * 1024 ** 3 }
  }

  async appendFile(path: string, data: Buffer): Promise<void> {
    this.allEvents.push(`append:${path}`)
    const event = path.endsWith('index.journal') ? 'journal.write' : 'segment.write'
    this.events.push(event)
    this.signal(event)
    await this.hooks[event]?.()
    this.files.set(path, Buffer.concat([this.files.get(path) ?? Buffer.alloc(0), data]))
  }

  async mkdir(path: string): Promise<void> {
    this.allEvents.push(`mkdir:${path}`)
  }

  async readFile(path: string): Promise<Buffer | null> {
    const value = this.files.get(path)

    return value === undefined ? null : Buffer.from(value)
  }

  async readRange(path: string, offset: number, length: number): Promise<Buffer | null> {
    const value = this.files.get(path)

    return value === undefined ? null : Buffer.from(value.subarray(offset, offset + length))
  }

  async rename(from: string, to: string): Promise<void> {
    this.allEvents.push(`rename:${from}:${to}`)
    const value = this.files.get(from)

    if (value === undefined) {
      throw new Error('missing_file')
    }

    this.files.set(to, value)
    this.files.delete(from)
  }

  async replaceFile(from: string, to: string): Promise<void> {
    this.allEvents.push(`replace:${from}:${to}`)
    const value = this.files.get(from)

    if (value === undefined) {
      throw new Error('missing_file')
    }

    this.files.set(to, value)
    this.files.delete(from)
  }

  async stat(path: string): Promise<number | null> {
    return this.files.get(path)?.length ?? null
  }

  async syncDirectory(path: string): Promise<void> {
    this.allEvents.push(`sync-directory:${path}`)
  }

  async syncFile(path: string): Promise<void> {
    this.allEvents.push(`sync-file:${path}`)

    if (!path.endsWith('index.journal') && !path.endsWith('active.segment')) {
      return
    }

    const event = path.endsWith('index.journal') ? 'journal.sync' : 'segment.sync'
    this.events.push(event)
    this.signal(event)
    await this.hooks[event]?.()
  }

  async truncateFile(path: string, length: number): Promise<void> {
    const value = this.files.get(path)

    if (value === undefined) {
      throw new Error('missing_file')
    }
    this.files.set(path, value.subarray(0, length))
  }

  async unlink(path: string): Promise<void> {
    this.allEvents.push(`unlink:${path}`)
    this.files.delete(path)
  }

  async writeFile(path: string, data: Buffer, options?: { exclusive?: boolean }): Promise<void> {
    this.allEvents.push(`write:${path}:${options?.exclusive === true ? 'exclusive' : 'replace'}`)
    this.files.set(path, Buffer.from(data))
  }

  async waitFor(event: string): Promise<void> {
    if (this.events.includes(event)) {
      return
    }

    await new Promise<void>(resolve => {
      const waiters = this.waiters.get(event) ?? []
      waiters.push(resolve)
      this.waiters.set(event, waiters)
    })
  }

  private signal(event: string): void {
    for (const resolve of this.waiters.get(event) ?? []) {
      resolve()
    }

    this.waiters.delete(event)
  }
}

class RangeOnlyTraceFileSystem extends FakeTraceFileSystem {
  readonly rangeReads: Array<{ length: number; offset: number; path: string }> = []
  rejectWholeReads = false
  readonly reportedSizes = new Map<string, number>()

  override async readFile(path: string): Promise<Buffer | null> {
    if (this.rejectWholeReads && (path.endsWith('active.segment') || path.endsWith('index.journal'))) {
      throw new Error('whole_file_read_forbidden')
    }

    return super.readFile(path)
  }

  async stat(path: string): Promise<number | null> {
    const contents = this.files.get(path)

    if (contents === undefined) {
      return null
    }

    return this.reportedSizes.get(path) ?? contents.length
  }

  async readRange(path: string, offset: number, length: number): Promise<Buffer | null> {
    this.rangeReads.push({ length, offset, path })
    const contents = this.files.get(path)

    if (contents === undefined) {
      return null
    }

    return Buffer.from(contents.subarray(offset, offset + length))
  }

  async truncateFile(path: string, length: number): Promise<void> {
    const contents = this.files.get(path)

    if (contents === undefined) {
      throw new Error('missing_file')
    }

    this.files.set(path, contents.subarray(0, length))
  }
}

function protector() {
  return createSafeStorageTraceKeyProtector({
    decryptString: ciphertext => ciphertext.toString('utf8'),
    encryptString: plaintext => Buffer.from(plaintext, 'utf8'),
    isEncryptionAvailable: () => true
  })
}

function options(overrides: Partial<Parameters<typeof TraceOutboxStore.open>[0]> = {}) {
  return {
    fs: new FakeTraceFileSystem(),
    groupCommitMs: 5,
    keyProtector: protector(),
    root: '/outbox',
    ...overrides
  }
}

function envelope(label: string): TraceEnvelopeInput {
  return {
    body: Buffer.from(`trace:${label}`),
    contentType: 'application/x-protobuf',
    entrypoint: 'desktop',
    hermesSessionId: `session-${label}`,
    owner: {
      accountId: '11111111-1111-4111-8111-111111111111',
      accountKey: 'account-11111111-1111-4111-8111-111111111111',
      installationId: '33333333-3333-4333-8333-333333333333',
      sessionId: '22222222-2222-4222-8222-222222222222'
    },
    runId: `run-${label}`,
    telemetrySchemaVersion: '1'
  }
}

function receipt(batchId: string, outcome: DurableReceipt['outcome']): DurableReceipt {
  return { batchId, outcome, receivedAt: 1_798_000_000_000 }
}

test('resolves every grouped enqueue only after segment sync then journal sync', async () => {
  const events: string[] = []
  const gate = deferred<void>()

  const fs = new FakeTraceFileSystem({
    'journal.sync': () => {
      events.push('journal.sync')
    },
    'journal.write': () => {
      events.push('journal.write')
    },
    'segment.sync': async () => {
      events.push('segment.sync')
      await gate.promise
    },
    'segment.write': () => {
      events.push('segment.write')
    }
  })

  const store = await TraceOutboxStore.open(options({ fs, groupCommitMs: 50 }))
  let acknowledged = false

  const first = store.enqueue(envelope('one')).then(() => {
    acknowledged = true
  })

  const second = store.enqueue(envelope('two'))

  await fs.waitFor('segment.sync')
  assert.equal(acknowledged, false)
  gate.resolve()
  await Promise.all([first, second])
  assert.deepEqual(events, ['segment.write', 'segment.write', 'segment.sync', 'journal.write', 'journal.sync'])
})

test('Gateway receipt can win before local commit without retaining payload bytes', async () => {
  const store = await TraceOutboxStore.open(options())
  const pending = store.beginEnqueue(envelope('online'))

  await pending.cancelForGatewayReceipt(receipt(pending.batchId, 'accepted'))
  assert.equal((await store.diagnostics()).payloadBytes, 0)
  assert.equal((await store.lookupReceipt(pending.batchId))?.outcome, 'accepted')
  await assert.rejects(pending.durable, /local_commit_cancelled/)
})

test('syncs an exclusive same-directory key temporary before renaming it into place', async () => {
  const fs = new FakeTraceFileSystem()
  await TraceOutboxStore.open(options({ fs }))

  const keyWrite = fs.allEvents.findIndex(
    event => event.startsWith('write:/outbox/key.json.tmp-') && event.endsWith(':exclusive')
  )

  const keySync = fs.allEvents.findIndex(event => event.startsWith('sync-file:/outbox/key.json.tmp-'))
  const keyRename = fs.allEvents.findIndex(event => event.startsWith('rename:/outbox/key.json.tmp-'))
  const directorySync = fs.allEvents.indexOf('sync-directory:/outbox')

  assert.ok(keyWrite >= 0)
  assert.ok(keyWrite < keySync)
  assert.ok(keySync < keyRename)
  assert.ok(keyRename < directorySync)
})

test('a receipt during a synced append creates a tombstone and makes the payload reclaimable', async () => {
  const gate = deferred<void>()
  const fs = new FakeTraceFileSystem({ 'segment.sync': () => gate.promise })
  const store = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1 }))
  const pending = store.beginEnqueue(envelope('post-append'))

  await fs.waitFor('segment.sync')
  const cancelled = pending.cancelForGatewayReceipt(receipt(pending.batchId, 'duplicate'))
  gate.resolve()
  await cancelled

  assert.equal((await store.diagnostics()).payloadBytes, 0)
  assert.equal((await store.lookupReceipt(pending.batchId))?.outcome, 'duplicate')
  await assert.doesNotReject(pending.durable)
})

test('writes key.json through a synced temporary file before accepting the first record on real disk', async () => {
  const tmpBase = resolve(process.cwd(), 'tmp')
  await mkdir(tmpBase, { recursive: true })
  const root = await mkdtemp(join(tmpBase, 'trace-outbox-'))

  try {
    const store = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    const batch = await store.enqueue(envelope('disk'))
    const key = JSON.parse(await readFile(join(root, 'key.json'), 'utf8')) as { wrappedKey: string; version: number }

    assert.equal(key.version, 1)
    assert.ok(key.wrappedKey.length > 0)
    assert.notEqual(key.wrappedKey, Buffer.alloc(32).toString('base64'))
    assert.equal(await store.lookupReceipt(batch.batchId), undefined)
    assert.ok((await store.diagnostics()).payloadBytes > 0)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('a segment or journal sync failure rejects every member of that commit group', async () => {
  const fs = new FakeTraceFileSystem({
    'journal.sync': () => {
      throw new Error('journal_sync_failed')
    }
  })

  const store = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1 }))

  await assert.rejects(
    Promise.all([store.enqueue(envelope('one')), store.enqueue(envelope('two'))]),
    /journal_sync_failed/
  )
})

test('a segment sync failure rejects every member before journal metadata is written', async () => {
  const fs = new FakeTraceFileSystem({
    'segment.sync': () => {
      throw new Error('segment_sync_failed')
    }
  })

  const store = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1 }))

  await assert.rejects(
    Promise.all([store.enqueue(envelope('one')), store.enqueue(envelope('two'))]),
    /segment_sync_failed/
  )
  assert.equal(fs.events.includes('journal.write'), false)
})

test('rejects invalid runtime input and invalid injected time before writing an outgoing record', async () => {
  const cases = [
    {
      input: { ...envelope('bad-input'), runId: '../bad' } as TraceEnvelopeInput,
      now: () => 1_798_000_000_000
    },
    { input: envelope('bad-now'), now: () => Number.NaN }
  ]

  for (const testCase of cases) {
    const fs = new FakeTraceFileSystem()
    const store = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1, now: testCase.now }))

    await assert.rejects(store.enqueue(testCase.input), /invalid_record_header/)
    assert.equal(fs.events.includes('segment.write'), false)
    assert.equal(fs.events.includes('journal.write'), false)
    assert.equal((await store.diagnostics()).payloadBytes, 0)
  }
})

test('coalesces concurrent identical Gateway receipts and rejects all callers if their receipt sync fails', async () => {
  const fs = new FakeTraceFileSystem({
    'journal.sync': () => {
      throw new Error('receipt_sync_failed')
    }
  })

  const store = await TraceOutboxStore.open(options({ fs }))
  const pending = store.beginEnqueue(envelope('receipt-failure'))
  const accepted = receipt(pending.batchId, 'accepted')
  const first = pending.cancelForGatewayReceipt(accepted)
  const second = pending.cancelForGatewayReceipt(accepted)

  assert.strictEqual(first, second)
  await assert.rejects(first, /receipt_sync_failed/)
  await assert.rejects(second, /receipt_sync_failed/)
  await assert.rejects(pending.durable, /receipt_sync_failed/)
  await assert.rejects(
    pending.cancelForGatewayReceipt(receipt(pending.batchId, 'duplicate')),
    /conflicting_gateway_receipt/
  )
})

test('rejects symlinked outbox root, segments, key, and journal paths before reading or writing through them', async () => {
  const tmpBase = resolve(process.cwd(), 'tmp')
  await mkdir(tmpBase, { recursive: true })
  const base = await mkdtemp(join(tmpBase, 'trace-outbox-symlink-'))

  try {
    const target = join(base, 'target')
    await mkdir(target)

    const cases = [
      { name: 'root', setup: async () => symlink(target, join(base, 'root')) },
      {
        name: 'segments',
        setup: async () => {
          const root = join(base, 'segments-root')
          await mkdir(root)
          await symlink(target, join(root, 'segments'))

          return root
        }
      },
      {
        name: 'key',
        setup: async () => {
          const root = join(base, 'key-root')
          await mkdir(root)
          await writeFile(join(base, 'key-target'), 'not-a-key')
          await symlink(join(base, 'key-target'), join(root, 'key.json'))

          return root
        }
      },
      {
        name: 'journal',
        setup: async () => {
          const root = join(base, 'journal-root')
          await mkdir(root)
          await writeFile(join(base, 'journal-target'), '')
          await symlink(join(base, 'journal-target'), join(root, 'index.journal'))

          return root
        }
      }
    ]

    for (const candidate of cases) {
      const configuredRoot = await candidate.setup()
      const root = typeof configuredRoot === 'string' ? configuredRoot : join(base, 'root')
      await assert.rejects(
        TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root }),
        /unsafe_trace_outbox_path/
      )
    }
  } finally {
    await rm(base, { force: true, recursive: true })
  }
})

test('drains an overdue next group immediately after a slow prior group releases', async () => {
  const gate = deferred<void>()
  let monotonicNow = 0
  const fs = new FakeTraceFileSystem({ 'segment.sync': () => gate.promise })
  const store = await TraceOutboxStore.open(options({ fs, groupCommitMs: 50, monotonicNow: () => monotonicNow }))
  const first = store.enqueue(envelope('slow-first'))

  await fs.waitFor('segment.sync')
  const second = store.enqueue(envelope('overdue-second'))
  monotonicNow = 51
  gate.resolve()
  await first
  await new Promise<void>(resolve => setTimeout(resolve, 20))

  assert.equal(fs.events.filter(event => event === 'segment.write').length, 2)
  await second
})

type CrashPoint = 'segment-tail' | 'journal-tail' | 'after-segment-sync' | 'during-send' | 'after-receipt'

interface CrashFixture {
  expectedPendingIds: string[]
  expectedPendingBodies: string[]
  journalLength: number
  segmentLength: number
}

async function temporaryOutboxDirectory(): Promise<string> {
  const tmpBase = resolve(process.cwd(), 'tmp')
  await mkdir(tmpBase, { recursive: true })

  return mkdtemp(join(tmpBase, 'trace-outbox-crash-'))
}

async function buildCrashFixture(root: string, crashPoint: CrashPoint): Promise<CrashFixture> {
  const store = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
  const commits = [store.beginEnqueue(envelope('one')), store.beginEnqueue(envelope('two'))]
  const batches = await Promise.all(commits.map(commit => commit.durable))
  const segmentPath = join(root, 'segments', 'active.segment')
  const journalPath = join(root, 'index.journal')
  const journalLength = (await readFile(journalPath)).length
  const segmentLength = (await readFile(segmentPath)).length

  if (crashPoint === 'segment-tail') {
    await appendFile(segmentPath, Buffer.from('torn-segment-tail', 'utf8'))
  }

  if (crashPoint === 'journal-tail') {
    await appendFile(journalPath, Buffer.from('{"checksum":"torn-journal-tail"', 'utf8'))
  }

  if (crashPoint === 'after-segment-sync') {
    await writeFile(journalPath, Buffer.alloc(0))
  }

  if (crashPoint === 'after-receipt') {
    await commits[0].cancelForGatewayReceipt(receipt(batches[0].batchId, 'accepted'))
  }

  return {
    expectedPendingIds: crashPoint === 'after-receipt' ? [batches[1].batchId] : batches.map(batch => batch.batchId),
    expectedPendingBodies: crashPoint === 'after-receipt' ? ['trace:two'] : ['trace:one', 'trace:two'],
    journalLength,
    segmentLength
  }
}

test.each<CrashPoint>(['segment-tail', 'journal-tail', 'after-segment-sync', 'during-send', 'after-receipt'])(
  'recovers deterministic real-disk state after %s',
  async crashPoint => {
    const root = await temporaryOutboxDirectory()

    try {
      const fixture = await buildCrashFixture(root, crashPoint)
      const recovered = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
      const diagnostics = await recovered.diagnostics()
      const eligible = await recovered.peekEligible(Number.MAX_SAFE_INTEGER)

      assert.equal(eligible?.body.toString('utf8'), fixture.expectedPendingBodies[0])
      const drained: string[] = []
      let current = recovered
      while (drained.length < fixture.expectedPendingIds.length) {
        const head = await current.peekEligible(Number.MAX_SAFE_INTEGER)
        assert.notEqual(head, undefined)
        drained.push(head.batchId)
        const journal = await TraceJournal.open({ fs: nodeTraceFileSystem, path: join(root, 'index.journal') })
        await journal.append([
          { op: 'receipt', batchId: head.batchId, outcome: 'accepted', receivedAt: 1_798_000_000_001 }
        ])
        await journal.sync()
        current = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
      }
      assert.deepEqual(drained, fixture.expectedPendingIds)
      assert.equal(new Set(drained).size, drained.length)
      assert.equal(await current.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
      assert.equal(diagnostics.pending, fixture.expectedPendingBodies.length)
      assert.equal(diagnostics.recoveredCorruptTail, crashPoint.endsWith('tail') ? 1 : 0)
      assert.equal(new Set(fixture.expectedPendingBodies).size, fixture.expectedPendingBodies.length)

      if (crashPoint === 'segment-tail') {
        assert.equal((await readFile(join(root, 'segments', 'active.segment'))).length, fixture.segmentLength)
        const third = await recovered.enqueue(envelope('three'))
        assert.equal(third.sequence, 2)
        assert.equal((await recovered.diagnostics()).pending, 3)
      }
    } finally {
      await rm(root, { force: true, recursive: true })
    }
  }
)

test('quarantines a non-tail segment corruption without scanning later untrusted records', async () => {
  const root = await temporaryOutboxDirectory()

  try {
    await buildCrashFixture(root, 'during-send')
    const segmentPath = join(root, 'segments', 'active.segment')
    const segment = Buffer.from(await readFile(segmentPath))
    const first = decodeSegmentRecord(segment, 0)

    assert.notEqual(first, null)
    segment[first.nextOffset - 1] ^= 0x01
    await writeFile(segmentPath, segment)

    const recovered = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })

    assert.equal(await recovered.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
    assert.equal((await recovered.diagnostics()).quarantined, 1)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('recovers and peeks through bounded range reads without whole segment or journal buffers', async () => {
  const fs = new RangeOnlyTraceFileSystem()
  const first = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1 }))
  await first.enqueue(envelope('bounded'))
  fs.rejectWholeReads = true

  const recovered = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1 }))
  const eligible = await recovered.peekEligible(Number.MAX_SAFE_INTEGER)

  assert.equal(eligible?.body.toString(), 'trace:bounded')
  assert.ok(fs.rangeReads.some(read => read.path.endsWith('index.journal')))
  assert.ok(fs.rangeReads.some(read => read.path.endsWith('active.segment')))
  assert.ok(fs.rangeReads.every(read => read.length <= 64 * 1024 * 1024 + 64 * 1024 + 64))
})

test('quarantines a sparse segment above capacity without reading its reported contents', async () => {
  const fs = new RangeOnlyTraceFileSystem()
  const initial = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1 }))
  await initial.enqueue(envelope('sparse'))
  const segmentPath = '/outbox/segments/active.segment'
  fs.reportedSizes.set(segmentPath, 3 * 1024 * 1024 * 1024)
  fs.rejectWholeReads = true

  const recovered = await TraceOutboxStore.open(options({ fs, groupCommitMs: 1 }))

  assert.equal((await recovered.diagnostics()).quarantined, 1)
  assert.equal(
    fs.rangeReads.some(read => read.path === segmentPath),
    false
  )
})

test('does not truncate a corrupt oversize prefix or scan past it', async () => {
  const root = await temporaryOutboxDirectory()
  try {
    await buildCrashFixture(root, 'during-send')
    const path = join(root, 'segments', 'active.segment')
    const valid = await readFile(path)
    const corrupt = Buffer.alloc(25)
    Buffer.from('ATOB').copy(corrupt)
    corrupt.writeUInt8(1, 4)
    corrupt.writeUInt32BE(64 * 1024 * 1024 + 1, 17)
    const fixture = Buffer.concat([valid.subarray(0, decodeSegmentRecord(valid, 0)!.nextOffset), corrupt, valid])
    await writeFile(path, fixture)

    const recovered = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    assert.deepEqual(await readFile(path), fixture)
    assert.equal((await recovered.diagnostics()).quarantined, 1)
    assert.equal(await recovered.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('evicts oldest telemetry at hard limits, dedupes locally, and never touches SessionDB', async () => {
  const root = await temporaryOutboxDirectory()
  const sessionDb = join(root, '..', 'hermes-state.db')
  await writeFile(sessionDb, 'conversation-truth', 'utf8')

  try {
    const store = await TraceOutboxStore.open(
      options({
        capacityBytes: 900,
        freeSpace: () => ({ available: 5 * 1024 ** 3, total: 20 * 1024 ** 3 }),
        root
      })
    )
    const one = await store.enqueue(envelope('one'))
    const two = await store.enqueue(envelope('two'))

    assert.equal((await store.diagnostics()).evictedCapacity, 1)
    const duplicate = await store.enqueue(envelope('two'))
    assert.equal(duplicate.batchId, two.batchId)
    assert.equal((await store.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, two.batchId)
    await store.quarantine(duplicate.batchId, 'payload_too_large')
    assert.equal((await store.diagnostics()).quarantined, 1)
    assert.equal(await readFile(sessionDb, 'utf8'), 'conversation-truth')
    assert.notEqual(one.batchId, duplicate.batchId)
  } finally {
    await rm(root, { force: true, recursive: true })
    await rm(sessionDb, { force: true })
  }
})

test('refreshes free space after deleting a fully terminal segment before admitting telemetry', async () => {
  const fs = new FakeTraceFileSystem()
  const originalUnlink = fs.unlink.bind(fs)
  let available = 5 * 1024 ** 3
  let freeSpaceCalls = 0

  fs.unlink = async path => {
    await originalUnlink(path)
    available = 5 * 1024 ** 3
  }

  const store = await TraceOutboxStore.open(
    options({
      capacityBytes: 900,
      freeSpace: () => {
        freeSpaceCalls += 1
        return { available, total: 20 * 1024 ** 3 }
      },
      fs
    })
  )
  await store.enqueue(envelope('first'))
  available = 1024 ** 3
  await store.enqueue(envelope('second'))

  assert.ok(freeSpaceCalls >= 3)
  assert.equal((await store.peekEligible(Number.MAX_SAFE_INTEGER))?.runId, 'run-second')
})

test('durably quarantines an oversize diagnostic input while normal admission rejects it', async () => {
  const oversized = { ...envelope('oversize'), body: Buffer.alloc(8 * 1024 * 1024 + 1, 0x61) }
  const store = await TraceOutboxStore.open(options())

  await assert.rejects(store.enqueue(oversized), /payload_too_large/)
  const quarantined = await store.quarantineInput(oversized, 'payload_too_large')

  assert.equal(await store.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
  assert.equal((await store.diagnostics()).quarantined, 1)
  assert.ok((await store.diagnostics()).payloadBytes > 0)
  assert.equal(quarantined.batchId.length, 36)
})

test('makes a receipt terminal only after its journal sync and bounds expired tombstones', async () => {
  const gate = deferred<void>()
  let blockReceiptSync = false
  const fs = new FakeTraceFileSystem({
    'journal.sync': async () => {
      if (blockReceiptSync) {
        await gate.promise
      }
    }
  })
  let now = 1_798_000_000_000
  const store = await TraceOutboxStore.open(options({ fs, now: () => now, retentionMs: 10 }))
  const batch = await store.enqueue(envelope('receipt-boundary'))
  blockReceiptSync = true
  const acknowledged = store.acknowledge(batch.batchId, receipt(batch.batchId, 'accepted'))

  await fs.waitFor('journal.sync')
  assert.equal((await store.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, batch.batchId)
  gate.resolve()
  await acknowledged
  assert.equal(await store.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
  now += 11
  assert.equal(await store.lookupReceipt(batch.batchId), undefined)
  assert.equal((await store.diagnostics()).tombstones, 0)
})

test('physically bounds tombstones and reclaims fully-terminal payloads during a long stream', async () => {
  const root = await temporaryOutboxDirectory()
  let now = 1_798_000_000_000
  const receiptCapacityBytes = 2_048
  const streamingOptions = {
    groupCommitMs: 1,
    isConversationStreaming: () => true,
    keyProtector: protector(),
    now: () => now,
    receiptCapacityBytes,
    receiptCapacityEntries: 3,
    retentionMs: 5,
    root
  }

  try {
    const store = await TraceOutboxStore.open(streamingOptions)
    const acknowledged: string[] = []

    for (let index = 0; index < 20; index += 1) {
      const batch = await store.enqueue(envelope(`stream-receipt-${index}`))
      acknowledged.push(batch.batchId)
      await store.acknowledge(batch.batchId, {
        batchId: batch.batchId,
        outcome: 'accepted',
        receivedAt: now
      })
      now += 1
    }

    const journal = await TraceJournal.open({ fs: nodeTraceFileSystem, path: join(root, 'index.journal') })
    const durable = await journal.recover()
    const durableReceipts = durable.operations.filter(operation => operation.op === 'receipt')
    const journalBytes = (await readFile(join(root, 'index.journal'))).length

    assert.ok(durableReceipts.length <= 3)
    assert.ok(journalBytes <= receiptCapacityBytes)
    assert.equal(durable.operations.filter(operation => operation.op === 'owner').length, 1)
    assert.equal(await nodeTraceFileSystem.stat(join(root, 'segments', 'active.segment')), null)

    const reopened = await TraceOutboxStore.open(streamingOptions)
    assert.equal(await reopened.lookupReceipt(acknowledged[0]), undefined)
    assert.equal((await reopened.lookupReceipt(acknowledged.at(-1)!))?.outcome, 'accepted')
    assert.ok((await reopened.diagnostics()).tombstones <= 3)

    const other = envelope('other-account-after-stream')
    other.owner = {
      ...other.owner,
      accountId: '44444444-4444-4444-8444-444444444444',
      accountKey: 'account-44444444-4444-4444-8444-444444444444'
    }
    await assert.rejects(reopened.enqueue(other), /trace_outbox_account_mismatch/)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('caps the durable encoded receipt lines by bytes independently of the entry limit', async () => {
  const root = await temporaryOutboxDirectory()
  const receiptCapacityBytes = 700
  const optionsForRoot = {
    groupCommitMs: 1,
    isConversationStreaming: () => true,
    keyProtector: protector(),
    receiptCapacityBytes,
    receiptCapacityEntries: 100,
    root
  }

  try {
    const store = await TraceOutboxStore.open(optionsForRoot)
    for (let index = 0; index < 8; index += 1) {
      const batch = await store.enqueue(envelope(`byte-capped-receipt-${index}`))
      await store.acknowledge(batch.batchId, receipt(batch.batchId, 'accepted'))
    }

    const journal = await TraceJournal.open({ fs: nodeTraceFileSystem, path: join(root, 'index.journal') })
    const operations = (await journal.recover()).operations
    const receipts = operations.filter(operation => operation.op === 'receipt')
    const receiptBytes = receipts.reduce((total, operation) => total + traceJournalOperationBytes(operation), 0)
    const nonReceiptBytes = operations
      .filter(operation => operation.op !== 'receipt')
      .reduce((total, operation) => total + traceJournalOperationBytes(operation), 0)
    const journalBytes = (await readFile(join(root, 'index.journal'))).length

    assert.ok(receipts.length < 8)
    assert.ok(receiptBytes <= receiptCapacityBytes)
    assert.equal(journalBytes, receiptBytes + nonReceiptBytes)
    assert.equal(await nodeTraceFileSystem.stat(join(root, 'segments', 'active.segment')), null)
    const reopened = await TraceOutboxStore.open(optionsForRoot)
    assert.equal((await reopened.diagnostics()).tombstoneBytes, receiptBytes)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('quarantines encrypted records and reports diagnostics when the persisted key is lost', async () => {
  const fs = new FakeTraceFileSystem()
  const first = await TraceOutboxStore.open(options({ fs }))
  await first.enqueue(envelope('key-loss'))
  const lostProtector = createSafeStorageTraceKeyProtector({
    decryptString: () => {
      throw new Error('lost_key')
    },
    encryptString: plaintext => Buffer.from(plaintext, 'utf8'),
    isEncryptionAvailable: () => true
  })

  const recovered = await TraceOutboxStore.open(options({ fs, keyProtector: lostProtector }))

  assert.equal((await recovered.diagnostics()).keyLost, 1)
  assert.equal((await recovered.diagnostics()).quarantined, 1)
  assert.equal(await recovered.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
})

test('replays durable quarantine and eviction terminal states after restart', async () => {
  const root = await temporaryOutboxDirectory()
  const evictionRoot = await temporaryOutboxDirectory()

  try {
    const store = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    const quarantined = await store.enqueue(envelope('restart-quarantine'))
    await store.quarantine(quarantined.batchId, 'payload_too_large')
    const recoveredQuarantine = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })

    assert.equal(await recoveredQuarantine.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
    assert.equal((await recoveredQuarantine.diagnostics()).quarantined, 1)

    const evicting = await TraceOutboxStore.open({
      capacityBytes: 900,
      freeSpace: () => ({ available: 5 * 1024 ** 3, total: 20 * 1024 ** 3 }),
      groupCommitMs: 1,
      keyProtector: protector(),
      root: evictionRoot
    })
    await evicting.enqueue(envelope('restart-evicted-one'))
    const live = await evicting.enqueue(envelope('restart-evicted-two'))
    const reopened = await TraceOutboxStore.open({
      capacityBytes: 900,
      freeSpace: () => ({ available: 5 * 1024 ** 3, total: 20 * 1024 ** 3 }),
      groupCommitMs: 1,
      keyProtector: protector(),
      root: evictionRoot
    })

    assert.equal((await reopened.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, live.batchId)
    assert.equal((await reopened.diagnostics()).evictedCapacity, 0)
  } finally {
    await rm(root, { force: true, recursive: true })
    await rm(evictionRoot, { force: true, recursive: true })
  }
})

test('compacts mixed segments only when conversation streaming is idle', async () => {
  const fs = new FakeTraceFileSystem()
  let streaming = true
  const store = await TraceOutboxStore.open(options({ fs, isConversationStreaming: () => streaming }))
  const first = await store.enqueue(envelope('compact-first'))
  const second = await store.enqueue(envelope('compact-second'))
  const path = '/outbox/segments/active.segment'
  const before = fs.files.get(path)!.length

  await store.acknowledge(first.batchId, receipt(first.batchId, 'accepted'))
  assert.equal(await store.compactIfIdle(), false)
  assert.equal(fs.files.get(path)!.length, before)
  streaming = false
  assert.equal(await store.compactIfIdle(), true)
  assert.ok(fs.files.get(path)!.length < before)
  assert.equal((await store.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, second.batchId)
})

test('rejects malformed acknowledgements without writing a receipt and binds a root to one account', async () => {
  const fs = new FakeTraceFileSystem()
  const store = await TraceOutboxStore.open(options({ fs }))
  const first = await store.enqueue(envelope('bound'))
  const before = fs.events.filter(event => event === 'journal.write').length

  await assert.rejects(
    store.acknowledge(first.batchId, { batchId: first.batchId, outcome: 'accepted', receivedAt: Number.NaN }),
    /invalid_trace_receipt/
  )
  assert.equal(fs.events.filter(event => event === 'journal.write').length, before)
  await assert.rejects(
    store.enqueue({
      ...envelope('other-account'),
      owner: {
        ...envelope('other-account').owner,
        accountId: '44444444-4444-4444-8444-444444444444',
        accountKey: 'account-44444444-4444-4444-8444-444444444444'
      }
    }),
    /trace_outbox_account_mismatch/
  )
})

test('uses the filesystem free-space seam by default and enforces its reserve', async () => {
  const fs = new FakeTraceFileSystem()
  let calls = 0
  fs.freeSpace = async () => {
    calls += 1
    return { available: 1024 ** 3, total: 20 * 1024 ** 3 }
  }
  const store = await TraceOutboxStore.open(options({ fs }))

  await assert.rejects(store.enqueue(envelope('reserve')), /storage_unavailable/)
  assert.ok(calls > 0)
})

test('reuses a reclaimed receipt batch without rewriting payload bytes across restart', async () => {
  const root = await temporaryOutboxDirectory()
  try {
    const first = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    const batch = await first.enqueue(envelope('receipt-safe'))
    await first.acknowledge(batch.batchId, receipt(batch.batchId, 'accepted'))
    const reopened = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    const duplicate = reopened.beginEnqueue(envelope('receipt-safe'))
    await duplicate.cancelForGatewayReceipt(receipt(batch.batchId, 'duplicate'))
    const next = await duplicate.durable

    assert.equal(next.batchId, batch.batchId)
    assert.equal(next.body.toString(), 'trace:receipt-safe')
    assert.equal(await reopened.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
    assert.equal((await reopened.diagnostics()).payloadBytes, 0)
    assert.equal((await reopened.lookupReceipt(batch.batchId))?.outcome, 'accepted')
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('persists root ownership after every payload and receipt tombstone is reclaimed or expired', async () => {
  const root = await temporaryOutboxDirectory()
  let now = 1_798_000_000_000
  try {
    const first = await TraceOutboxStore.open({
      groupCommitMs: 1,
      keyProtector: protector(),
      now: () => now,
      retentionMs: 10,
      root
    })
    const batch = await first.enqueue(envelope('owner-only'))
    await first.acknowledge(batch.batchId, receipt(batch.batchId, 'accepted'))
    now += 11
    assert.equal(await first.lookupReceipt(batch.batchId), undefined)
    await first.compactIfIdle()

    const reopened = await TraceOutboxStore.open({
      groupCommitMs: 1,
      keyProtector: protector(),
      now: () => now,
      retentionMs: 10,
      root
    })
    const other = envelope('other-owner-after-reclaim')
    other.owner = {
      ...other.owner,
      accountId: '44444444-4444-4444-8444-444444444444',
      accountKey: 'account-44444444-4444-4444-8444-444444444444'
    }

    await assert.rejects(reopened.enqueue(other), /trace_outbox_account_mismatch/)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('deduplicates a Gateway-first receipt after restart without creating a segment', async () => {
  const root = await temporaryOutboxDirectory()
  try {
    const first = await TraceOutboxStore.open({ groupCommitMs: 50, keyProtector: protector(), root })
    const pending = first.beginEnqueue(envelope('gateway-first-restart'))
    await pending.cancelForGatewayReceipt(receipt(pending.batchId, 'accepted'))
    await assert.rejects(pending.durable, /local_commit_cancelled/)

    const reopened = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    const duplicate = await reopened.enqueue(envelope('gateway-first-restart'))

    assert.equal(duplicate.batchId, pending.batchId)
    assert.equal((await reopened.diagnostics()).payloadBytes, 0)
    assert.equal(await reopened.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('upgrades a legacy receipt by recovering its identity from the retained encrypted record', async () => {
  const root = await temporaryOutboxDirectory()
  try {
    const first = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    const batch = await first.enqueue(envelope('legacy-receipt'))
    const journal = await TraceJournal.open({ fs: nodeTraceFileSystem, path: join(root, 'index.journal') })
    const recovered = await journal.recover()
    await journal.replace([
      ...recovered.operations.filter(operation => operation.op === 'pending'),
      { op: 'receipt', batchId: batch.batchId, outcome: 'accepted', receivedAt: 1_798_000_000_001 }
    ])

    const upgraded = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    assert.equal((await upgraded.lookupReceipt(batch.batchId))?.outcome, 'accepted')
    await upgraded.compactIfIdle()
    const reopened = await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), root })
    const duplicate = await reopened.enqueue(envelope('legacy-receipt'))

    assert.equal(duplicate.batchId, batch.batchId)
    assert.equal((await reopened.diagnostics()).payloadBytes, 0)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('reads an ownerless legacy receipt but fails closed for new account admission', async () => {
  const root = await temporaryOutboxDirectory()
  let now = 1_798_000_000_000
  try {
    await TraceOutboxStore.open({ groupCommitMs: 1, keyProtector: protector(), now: () => now, retentionMs: 10, root })
    const journal = await TraceJournal.open({ fs: nodeTraceFileSystem, path: join(root, 'index.journal') })
    await journal.append([{ op: 'receipt', batchId: 'legacy-ownerless', outcome: 'accepted', receivedAt: now }])
    await journal.sync()

    const reopened = await TraceOutboxStore.open({
      groupCommitMs: 1,
      keyProtector: protector(),
      now: () => now,
      retentionMs: 10,
      root
    })
    assert.equal((await reopened.lookupReceipt('legacy-ownerless'))?.outcome, 'accepted')
    await assert.rejects(reopened.enqueue(envelope('unsafe-owner-guess')), /trace_outbox_account_unknown/)
    now += 11
    assert.equal(await reopened.lookupReceipt('legacy-ownerless'), undefined)
    await reopened.compactIfIdle()
    const afterGc = await TraceOutboxStore.open({
      groupCommitMs: 1,
      keyProtector: protector(),
      now: () => now,
      retentionMs: 10,
      root
    })
    await assert.rejects(afterGc.enqueue(envelope('unsafe-after-gc')), /trace_outbox_account_unknown/)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('keeps repeated eviction journal state bounded across reopen', async () => {
  const root = await temporaryOutboxDirectory()
  try {
    const optionsForRoot = {
      capacityBytes: 900,
      freeSpace: () => ({ available: 5 * 1024 ** 3, total: 20 * 1024 ** 3 }),
      groupCommitMs: 1,
      keyProtector: protector(),
      root
    }
    let store = await TraceOutboxStore.open(optionsForRoot)
    for (let index = 0; index < 5; index += 1) {
      await store.enqueue(envelope(`evict-bound-${index}`))
      store = await TraceOutboxStore.open(optionsForRoot)
    }
    const journal = await readFile(join(root, 'index.journal'), 'utf8')
    assert.ok(journal.length < 4_096)
    assert.notEqual(await store.peekEligible(Number.MAX_SAFE_INTEGER), undefined)
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('serializes a gated compaction before a concurrent enqueue and preserves both records after reopen', async () => {
  const fs = new FakeTraceFileSystem()
  let streaming = true
  const gate = deferred<void>()
  const entered = deferred<void>()
  const originalReplace = fs.replaceFile.bind(fs)
  fs.replaceFile = async (from, to) => {
    if (to.endsWith('active.segment')) {
      entered.resolve()
      await gate.promise
    }
    await originalReplace(from, to)
  }
  const store = await TraceOutboxStore.open(options({ fs, isConversationStreaming: () => streaming }))
  const first = await store.enqueue(envelope('lock-first'))
  const second = await store.enqueue(envelope('lock-second'))
  await store.acknowledge(first.batchId, receipt(first.batchId, 'accepted'))
  streaming = false
  const compacting = store.compactIfIdle()
  await entered.promise
  const third = store.enqueue(envelope('lock-third'))
  gate.resolve()
  await compacting
  await third
  const recovered = await TraceOutboxStore.open(options({ fs }))
  assert.equal((await recovered.peekEligible(Number.MAX_SAFE_INTEGER))?.batchId, second.batchId)
  await recovered.acknowledge(second.batchId, receipt(second.batchId, 'accepted'))
  assert.equal((await recovered.peekEligible(Number.MAX_SAFE_INTEGER))?.runId, 'run-lock-third')
})
