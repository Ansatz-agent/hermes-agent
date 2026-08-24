import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import { test } from 'vitest'

import { canonicalJson, type TraceFileSystem, TraceJournal } from './trace-outbox-journal'

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
