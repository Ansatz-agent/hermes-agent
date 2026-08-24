import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'

import { test } from 'vitest'

import { createSafeStorageTraceKeyProtector } from './trace-outbox-crypto'
import { type TraceFileSystem } from './trace-outbox-journal'
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

  async rename(from: string, to: string): Promise<void> {
    this.allEvents.push(`rename:${from}:${to}`)
    const value = this.files.get(from)

    if (value === undefined) {
      throw new Error('missing_file')
    }

    this.files.set(to, value)
    this.files.delete(from)
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
