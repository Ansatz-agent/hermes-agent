import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { test } from 'vitest'

import { canonicalJson, nodeTraceFileSystem, type TraceFileSystem, TraceJournal } from './trace-outbox-journal'

class MemoryFileSystem implements TraceFileSystem {
  readonly files = new Map<string, Buffer>()

  async freeSpace(): Promise<{ available: number; total: number }> {
    return { available: 20 * 1024 ** 3, total: 20 * 1024 ** 3 }
  }

  async appendFile(path: string, data: Buffer): Promise<void> {
    this.files.set(path, Buffer.concat([this.files.get(path) ?? Buffer.alloc(0), data]))
  }

  async mkdir(): Promise<void> {}

  async readFile(path: string): Promise<Buffer | null> {
    const value = this.files.get(path)

    return value === undefined ? null : Buffer.from(value)
  }

  async readRange(path: string, offset: number, length: number): Promise<Buffer | null> {
    const value = this.files.get(path)

    return value === undefined ? null : Buffer.from(value.subarray(offset, offset + length))
  }

  async rename(from: string, to: string): Promise<void> {
    const value = this.files.get(from)

    if (value === undefined) {
      throw new Error('missing_file')
    }

    this.files.set(to, value)
    this.files.delete(from)
  }

  async replaceFile(from: string, to: string): Promise<void> {
    await this.rename(from, to)
  }

  async stat(path: string): Promise<number | null> {
    return this.files.get(path)?.length ?? null
  }

  async syncDirectory(): Promise<void> {}

  async syncFile(): Promise<void> {}

  async truncateFile(path: string, length: number): Promise<void> {
    const value = this.files.get(path)

    if (value === undefined) {
      throw new Error('missing_file')
    }

    this.files.set(path, value.subarray(0, length))
  }

  async unlink(path: string): Promise<void> {
    this.files.delete(path)
  }

  async writeFile(path: string, data: Buffer): Promise<void> {
    this.files.set(path, Buffer.from(data))
  }
}

test('replays checksummed canonical operations and ignores only a torn final line', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const journal = await TraceJournal.open({ fs, path })

  await journal.append([
    { op: 'pending', batchId: 'batch-1', createdAt: 1, length: 7, offset: 0, segment: 'active.segment', sequence: 1 },
    { op: 'receipt', batchId: 'batch-1', outcome: 'accepted', receivedAt: 2 }
  ])
  await journal.sync()

  const complete = await journal.recover()
  assert.deepEqual(complete, {
    operations: [
      { op: 'pending', batchId: 'batch-1', createdAt: 1, length: 7, offset: 0, segment: 'active.segment', sequence: 1 },
      { op: 'receipt', batchId: 'batch-1', outcome: 'accepted', receivedAt: 2 }
    ],
    recoveredTornTail: false
  })

  await fs.appendFile(path, Buffer.from('{"checksum":"partial"', 'utf8'))
  const torn = await journal.recover()
  assert.equal(torn.recoveredTornTail, true)
  assert.deepEqual(torn.operations, complete.operations)
})

test('rejects a checksum mismatch before replaying a complete journal line', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const operation = { op: 'receipt', batchId: 'batch-1', outcome: 'duplicate', receivedAt: 2 } as const
  const checksum = createHash('sha256').update(canonicalJson(operation)).digest('hex')

  await fs.appendFile(
    path,
    Buffer.from(`${canonicalJson({ checksum: `${checksum.slice(0, -1)}0`, operation })}\n`, 'utf8')
  )
  const journal = await TraceJournal.open({ fs, path })

  await assert.rejects(journal.recover(), /invalid_journal_checksum/)
})

test('replays typed terminal quarantine and eviction operations without treating them as receipts', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const journal = await TraceJournal.open({ fs, path })

  await journal.append([
    { op: 'terminal', batchId: 'batch-quarantine', errorClass: 'payload_too_large', terminal: 'quarantined' },
    { op: 'terminal', batchId: 'batch-evicted', terminal: 'evicted' }
  ])
  await journal.sync()

  assert.deepEqual(await journal.recover(), {
    operations: [
      { op: 'terminal', batchId: 'batch-quarantine', errorClass: 'payload_too_large', terminal: 'quarantined' },
      { op: 'terminal', batchId: 'batch-evicted', terminal: 'evicted' }
    ],
    recoveredTornTail: false
  })
})

test('accepts legacy receipts but requires canonical identity fields on extended receipts', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const journal = await TraceJournal.open({ fs, path })
  const legacy = { op: 'receipt', batchId: 'legacy-batch', outcome: 'accepted', receivedAt: 2 } as const

  const extended = {
    op: 'receipt',
    accountKey: 'account-11111111-1111-4111-8111-111111111111',
    batchId: 'batch-extended',
    dedupeKey: 'a'.repeat(64),
    outcome: 'duplicate',
    receivedAt: 3
  } as const

  await journal.append([legacy, extended])
  assert.deepEqual((await journal.recover()).operations, [legacy, extended])

  for (const malformed of [
    { ...extended, accountKey: 'account-11111111-1111-1111-111111111111' },
    { ...extended, dedupeKey: `${'a'.repeat(64)}suffix` },
    { ...extended, extra: true }
  ]) {
    const checksum = createHash('sha256').update(canonicalJson(malformed)).digest('hex')
    fs.files.set(path, Buffer.from(`${canonicalJson({ checksum, operation: malformed })}\n`, 'utf8'))
    await assert.rejects(journal.recover(), /invalid_journal_line/)
  }
})

test('recovers a journal larger than 128 KiB whose lines cross chunk boundaries', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const journal = await TraceJournal.open({ fs, path })

  const operations = Array.from({ length: 900 }, (_, index) => ({
    op: 'receipt' as const,
    batchId: `batch-${'x'.repeat(120)}-${index}`,
    outcome: 'accepted' as const,
    receivedAt: index
  }))

  await journal.append(operations)
  const size = fs.files.get(path)?.length ?? 0
  assert.ok(size > 128 * 1024, `journal must exceed two recovery chunks, got ${size}`)

  const recovered = await journal.recover()
  assert.equal(recovered.recoveredTornTail, false)
  assert.equal(recovered.operations.length, operations.length)
  assert.deepEqual(recovered.operations, operations)
})

test('rejects an individual journal line larger than one recovery chunk', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const journal = await TraceJournal.open({ fs, path })

  const oversized = {
    op: 'receipt' as const,
    batchId: 'x'.repeat(66 * 1024),
    outcome: 'accepted' as const,
    receivedAt: 1
  }

  const checksum = createHash('sha256').update(canonicalJson(oversized)).digest('hex')

  await fs.appendFile(path, Buffer.from(`${canonicalJson({ checksum, operation: oversized })}\n`, 'utf8'))
  await journal.append([{ op: 'receipt', batchId: 'after-oversized', outcome: 'accepted', receivedAt: 2 }])
  await assert.rejects(journal.recover(), /trace_outbox_journal_line_too_large/)

  const torn = new MemoryFileSystem()
  await torn.appendFile(path, Buffer.from('{'.repeat(70 * 1024), 'utf8'))
  const tornJournal = await TraceJournal.open({ fs: torn, path })
  await assert.rejects(tornJournal.recover(), /trace_outbox_journal_line_too_large/)
})

test('replays a strict persistent owner binding operation', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const journal = await TraceJournal.open({ fs, path })
  const owner = { op: 'owner', accountKey: `legacy-${'b'.repeat(64)}` } as const

  await journal.append([owner])
  assert.deepEqual((await journal.recover()).operations, [owner])

  await journal.replace([{ op: 'owner', accountKey: null }])
  assert.deepEqual((await journal.recover()).operations, [{ op: 'owner', accountKey: null }])
})

test('creates an absent journal on its first durable replacement', async () => {
  const root = await mkdtemp(join(tmpdir(), 'ansatz-trace-journal-'))
  const path = join(root, 'index.journal')
  const owner = { op: 'owner', accountKey: null } as const

  try {
    const journal = await TraceJournal.open({ fs: nodeTraceFileSystem, path })

    await journal.replace([owner])

    assert.deepEqual((await journal.recover()).operations, [owner])
  } finally {
    await rm(root, { force: true, recursive: true })
  }
})

test('journal compaction scratch is deterministic, crash-recoverable, and cleaned on failure', async () => {
  const fs = new MemoryFileSystem()
  const path = '/outbox/index.journal'
  const journal = await TraceJournal.open({ fs, path })
  const owner = { op: 'owner', accountKey: null } as const

  await journal.append([owner])

  // A crash orphan from a previous process is removed on recovery.
  fs.files.set(`${path}.compact`, Buffer.from('stale-scratch'))
  await journal.recover()
  assert.equal(fs.files.has(`${path}.compact`), false)

  // The scratch path is bounded and deterministic.
  const scratchWrites: string[] = []
  const originalWrite = fs.writeFile.bind(fs)

  fs.writeFile = async (writePath, data) => {
    scratchWrites.push(writePath)
    await originalWrite(writePath, data)
  }

  fs.files.set(`${path}.compact`, Buffer.from('stale-scratch'))
  await journal.replace([owner])
  assert.deepEqual(scratchWrites, [`${path}.compact`])
  assert.deepEqual((await journal.recover()).operations, [owner])
  assert.equal(fs.files.has(`${path}.compact`), false)

  // An ordinary failure cleans its own scratch and stays retryable.
  const originalReplace = fs.replaceFile.bind(fs)
  let failNextReplace = true

  fs.replaceFile = async (from, to) => {
    if (failNextReplace) {
      failNextReplace = false
      throw new Error('injected_replace_failure')
    }

    await originalReplace(from, to)
  }

  await assert.rejects(journal.replace([owner]), /injected_replace_failure/)
  assert.deepEqual(
    [...fs.files.keys()].filter(key => key.includes('.compact')),
    []
  )
  await journal.replace([owner])
  assert.deepEqual(
    [...fs.files.keys()].filter(key => key.includes('.compact')),
    []
  )
})
