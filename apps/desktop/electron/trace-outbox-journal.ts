import { createHash } from 'node:crypto'
import { constants, type Stats } from 'node:fs'
import { lstat, mkdir, open, rename, statfs, unlink } from 'node:fs/promises'
import { type FileHandle } from 'node:fs/promises'
import { dirname } from 'node:path'

import { isCanonicalTraceAccountKey } from './trace-outbox-types'

export interface TraceFileSystem {
  appendFile(path: string, data: Buffer): Promise<void>
  freeSpace(path: string): Promise<{ available: number; total: number }>
  mkdir(path: string): Promise<void>
  readFile(path: string): Promise<Buffer | null>
  readRange(path: string, offset: number, length: number): Promise<Buffer | null>
  rename(from: string, to: string): Promise<void>
  replaceFile(from: string, to: string): Promise<void>
  stat(path: string): Promise<number | null>
  syncDirectory(path: string): Promise<void>
  syncFile(path: string): Promise<void>
  truncateFile(path: string, length: number): Promise<void>
  unlink(path: string): Promise<void>
  writeFile(path: string, data: Buffer, options?: { exclusive?: boolean }): Promise<void>
}

const UNSAFE_PATH = 'unsafe_trace_outbox_path'
const PRIVATE_MODE_MASK = 0o077
const NO_FOLLOW = constants.O_NOFOLLOW ?? 0
const JOURNAL_CHUNK_BYTES = 64 * 1024
const MAX_JOURNAL_BYTES = 2 * 1024 * 1024 * 1024

function currentUid(): number | null {
  return typeof process.getuid === 'function' ? process.getuid() : null
}

function assertSafeStats(stats: Stats, type: 'directory' | 'file'): void {
  if (stats.isSymbolicLink() || (type === 'directory' ? !stats.isDirectory() : !stats.isFile())) {
    throw new Error(UNSAFE_PATH)
  }

  const uid = currentUid()

  if ((uid !== null && stats.uid !== uid) || (stats.mode & PRIVATE_MODE_MASK) !== 0) {
    throw new Error(UNSAFE_PATH)
  }
}

async function assertSafePath(path: string, type: 'directory' | 'file'): Promise<void> {
  try {
    assertSafeStats(await lstat(path), type)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      throw error
    }

    if ((error as Error).message === UNSAFE_PATH) {
      throw error
    }

    throw new Error(UNSAFE_PATH)
  }
}

async function openSafeFile(path: string, flags: number): Promise<FileHandle> {
  let handle: FileHandle

  try {
    handle = await open(path, flags | NO_FOLLOW, 0o600)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ELOOP') {
      throw new Error(UNSAFE_PATH)
    }

    throw error
  }

  try {
    assertSafeStats(await handle.stat(), 'file')

    return handle
  } catch (error) {
    await handle.close()
    throw error
  }
}

export const nodeTraceFileSystem: TraceFileSystem = {
  async freeSpace(path: string): Promise<{ available: number; total: number }> {
    const stats = await statfs(path)
    const blockSize = Number(stats.bsize)
    const availableBlocks = Number(stats.bavail)
    const totalBlocks = Number(stats.blocks)

    if (
      !Number.isSafeInteger(blockSize) ||
      !Number.isSafeInteger(availableBlocks) ||
      !Number.isSafeInteger(totalBlocks)
    ) {
      throw new Error('invalid_trace_outbox_free_space')
    }

    const available = blockSize * availableBlocks
    const total = blockSize * totalBlocks
    if (!Number.isSafeInteger(available) || !Number.isSafeInteger(total) || available < 0 || total < 0) {
      throw new Error('invalid_trace_outbox_free_space')
    }

    return { available, total }
  },
  async appendFile(path: string, data: Buffer): Promise<void> {
    const handle = await openSafeFile(path, constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT)

    try {
      await handle.writeFile(data)
    } finally {
      await handle.close()
    }
  },
  async mkdir(path: string): Promise<void> {
    try {
      await mkdir(path, { mode: 0o700, recursive: true })
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {
        throw error
      }
    }

    await assertSafePath(path, 'directory')
  },
  async readFile(path: string): Promise<Buffer | null> {
    try {
      await assertSafePath(path, 'file')
      const handle = await openSafeFile(path, constants.O_RDONLY)

      try {
        return await handle.readFile()
      } finally {
        await handle.close()
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        return null
      }

      throw error
    }
  },
  async readRange(path: string, offset: number, length: number): Promise<Buffer | null> {
    if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0) {
      throw new RangeError('invalid_trace_outbox_range')
    }

    try {
      await assertSafePath(path, 'file')
      const handle = await openSafeFile(path, constants.O_RDONLY)

      try {
        const size = (await handle.stat()).size

        if (offset > size || length > size - offset) {
          return null
        }
        const output = Buffer.allocUnsafe(length)
        let cursor = 0

        while (cursor < length) {
          const { bytesRead } = await handle.read(output, cursor, length - cursor, offset + cursor)

          if (bytesRead === 0) {
            return null
          }
          cursor += bytesRead
        }

        return output
      } finally {
        await handle.close()
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        return null
      }
      throw error
    }
  },
  async rename(from: string, to: string): Promise<void> {
    await assertSafePath(from, 'file')

    try {
      await lstat(to)
      throw new Error(UNSAFE_PATH)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        throw error
      }
    }

    await rename(from, to)
    await assertSafePath(to, 'file')
  },
  async replaceFile(from: string, to: string): Promise<void> {
    await assertSafePath(from, 'file')
    await assertSafePath(to, 'file')
    await rename(from, to)
    await assertSafePath(to, 'file')
  },
  async stat(path: string): Promise<number | null> {
    try {
      await assertSafePath(path, 'file')

      return (await lstat(path)).size
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        return null
      }
      throw error
    }
  },
  async syncDirectory(path: string): Promise<void> {
    await assertSafePath(path, 'directory')
    const handle = await open(path, constants.O_RDONLY | constants.O_DIRECTORY | NO_FOLLOW)

    try {
      await handle.sync()
    } finally {
      await handle.close()
    }
  },
  async syncFile(path: string): Promise<void> {
    await assertSafePath(path, 'file')
    const handle = await openSafeFile(path, constants.O_RDONLY)

    try {
      await handle.sync()
    } finally {
      await handle.close()
    }
  },
  async truncateFile(path: string, length: number): Promise<void> {
    const handle = await openSafeFile(path, constants.O_WRONLY)

    try {
      await handle.truncate(length)
    } finally {
      await handle.close()
    }
  },
  async unlink(path: string): Promise<void> {
    await assertSafePath(path, 'file')
    await unlink(path)
  },
  async writeFile(path: string, data: Buffer, options?: { exclusive?: boolean }): Promise<void> {
    const flags =
      constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | (options?.exclusive ? constants.O_EXCL : 0)

    const handle = await openSafeFile(path, flags)

    try {
      await handle.writeFile(data)
    } finally {
      await handle.close()
    }
  }
}

export type TraceJournalOperation =
  | TraceJournalOwnerOperation
  | TraceJournalPendingOperation
  | TraceJournalReceiptOperation
  | TraceJournalTerminalOperation

export interface TraceJournalOwnerOperation {
  op: 'owner'
  accountKey: string | null
}

export interface TraceJournalPendingOperation {
  op: 'pending'
  batchId: string
  createdAt: number
  length: number
  offset: number
  segment: string
  sequence: number
}

export interface TraceJournalReceiptOperation {
  op: 'receipt'
  batchId: string
  accountKey?: string
  dedupeKey?: string
  entrypoint?: string
  hermesSessionId?: string
  outcome: 'accepted' | 'duplicate'
  receivedAt: number
  runId?: string
  payloadSha256?: string
}

export interface TraceJournalTerminalOperation {
  op: 'terminal'
  batchId: string
  errorClass?: string
  terminal: 'evicted' | 'quarantined'
}

export interface TraceJournalRecovery {
  operations: TraceJournalOperation[]
  recoveredTornTail: boolean
}

export interface TraceJournalOptions {
  fs: TraceFileSystem
  path: string
}

interface TraceJournalLine {
  checksum: string
  operation: TraceJournalOperation
}

function canonicalValue(value: unknown): string {
  if (value === null || typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') {
    return JSON.stringify(value)
  }

  if (Array.isArray(value)) {
    return `[${value.map(canonicalValue).join(',')}]`
  }

  if (value === null || typeof value !== 'object') {
    throw new TypeError('unsupported_canonical_json_value')
  }

  const object = value as Record<string, unknown>

  return `{${Object.keys(object)
    .sort()
    .map(key => `${JSON.stringify(key)}:${canonicalValue(object[key])}`)
    .join(',')}}`
}

export function canonicalJson(value: unknown): string {
  return canonicalValue(value)
}

function operationChecksum(operation: TraceJournalOperation): string {
  return createHash('sha256').update(canonicalJson(operation)).digest('hex')
}

function encodeOperationLine(operation: TraceJournalOperation): string {
  return `${canonicalJson({ checksum: operationChecksum(operation), operation })}\n`
}

export function traceJournalOperationBytes(operation: TraceJournalOperation): number {
  return Buffer.byteLength(encodeOperationLine(operation), 'utf8')
}

function isPendingOperation(value: unknown): value is TraceJournalPendingOperation {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const operation = value as Record<string, unknown>

  return (
    operation.op === 'pending' &&
    typeof operation.batchId === 'string' &&
    typeof operation.segment === 'string' &&
    typeof operation.createdAt === 'number' &&
    typeof operation.length === 'number' &&
    typeof operation.offset === 'number' &&
    typeof operation.sequence === 'number' &&
    Number.isSafeInteger(operation.createdAt) &&
    Number.isSafeInteger(operation.length) &&
    Number.isSafeInteger(operation.offset) &&
    Number.isSafeInteger(operation.sequence) &&
    operation.createdAt >= 0 &&
    operation.length >= 0 &&
    operation.offset >= 0 &&
    operation.sequence >= 0
  )
}

function isReceiptOperation(value: unknown): value is TraceJournalReceiptOperation {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const operation = value as Record<string, unknown>

  const legacy =
    operation.op === 'receipt' &&
    typeof operation.batchId === 'string' &&
    (operation.outcome === 'accepted' || operation.outcome === 'duplicate') &&
    typeof operation.receivedAt === 'number' &&
    Number.isSafeInteger(operation.receivedAt) &&
    operation.receivedAt >= 0

  if (!legacy) {
    return false
  }

  const keys = Object.keys(operation)

  if (keys.length === 4) {
    return true
  }

  const extended =
    isCanonicalTraceAccountKey(operation.accountKey) &&
    typeof operation.dedupeKey === 'string' &&
    /^[0-9a-f]{64}$/.test(operation.dedupeKey)

  return (
    (keys.length === 6 && extended) ||
    (keys.length === 10 &&
      extended &&
      ['cli', 'dashboard', 'desktop', 'voice'].includes(String(operation.entrypoint)) &&
      typeof operation.hermesSessionId === 'string' &&
      typeof operation.runId === 'string' &&
      typeof operation.payloadSha256 === 'string' &&
      /^[0-9a-f]{64}$/.test(operation.payloadSha256))
  )
}

function isOwnerOperation(value: unknown): value is TraceJournalOwnerOperation {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const operation = value as Record<string, unknown>

  return (
    operation.op === 'owner' &&
    Object.keys(operation).length === 2 &&
    (operation.accountKey === null || isCanonicalTraceAccountKey(operation.accountKey))
  )
}

function isTerminalOperation(value: unknown): value is TraceJournalTerminalOperation {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const operation = value as Record<string, unknown>

  if (
    operation.op !== 'terminal' ||
    typeof operation.batchId !== 'string' ||
    operation.batchId.length === 0 ||
    operation.batchId.length > 128 ||
    (operation.terminal !== 'evicted' && operation.terminal !== 'quarantined')
  ) {
    return false
  }

  if (operation.terminal === 'evicted') {
    return Object.keys(operation).length === 3
  }

  return (
    Object.keys(operation).length === 4 &&
    typeof operation.errorClass === 'string' &&
    /^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$/.test(operation.errorClass)
  )
}

function isOperation(value: unknown): value is TraceJournalOperation {
  return isOwnerOperation(value) || isPendingOperation(value) || isReceiptOperation(value) || isTerminalOperation(value)
}

function parseLine(line: Buffer): TraceJournalOperation {
  let parsed: unknown

  try {
    parsed = JSON.parse(line.toString('utf8'))
  } catch {
    throw new Error('invalid_journal_line')
  }

  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('invalid_journal_line')
  }

  const journalLine = parsed as Partial<TraceJournalLine>

  if (
    typeof journalLine.checksum !== 'string' ||
    !/^[0-9a-f]{64}$/.test(journalLine.checksum) ||
    !isOperation(journalLine.operation)
  ) {
    throw new Error('invalid_journal_line')
  }

  if (operationChecksum(journalLine.operation) !== journalLine.checksum) {
    throw new Error('invalid_journal_checksum')
  }

  return journalLine.operation
}

export class TraceJournal {
  static async open(options: TraceJournalOptions): Promise<TraceJournal> {
    return new TraceJournal(options)
  }

  private recoveredTornTailOffset: number | null = null
  private unavailable = false

  private constructor(private readonly options: TraceJournalOptions) {}

  async append(operations: readonly TraceJournalOperation[]): Promise<number> {
    this.assertAvailable()

    if (operations.length === 0) {
      return 0
    }

    const encoded = Buffer.from(operations.map(operation => encodeOperationLine(operation)).join(''), 'utf8')

    await this.options.fs.appendFile(this.options.path, encoded)

    return encoded.length
  }

  // Appends and fsyncs as one protocol. If the fsync (or a partial append)
  // fails, the appended bytes may still become durable during a later sync,
  // so the tail is truncated back to the pre-append length and that rollback
  // is itself fsynced. If the rollback cannot be proven durable, the journal
  // becomes unavailable until reopen instead of guessing.
  async appendDurable(operations: readonly TraceJournalOperation[]): Promise<number> {
    this.assertAvailable()

    if (operations.length === 0) {
      return 0
    }

    const priorLength = (await this.options.fs.stat(this.options.path)) ?? 0

    try {
      const appended = await this.append(operations)
      await this.options.fs.syncFile(this.options.path)

      return appended
    } catch (error) {
      try {
        const currentLength = await this.options.fs.stat(this.options.path)

        if (currentLength !== null && currentLength !== priorLength) {
          await this.options.fs.truncateFile(this.options.path, priorLength)
          await this.options.fs.syncFile(this.options.path)
        }
      } catch {
        this.unavailable = true
        throw new Error('trace_outbox_journal_ambiguous')
      }

      throw error
    }
  }

  private assertAvailable(): void {
    if (this.unavailable) {
      throw new Error('trace_outbox_journal_unavailable')
    }
  }

  // The single deterministic scratch path bounds compaction disk usage: a
  // crash can orphan at most one file, which the next open (or the next
  // rewrite) removes before reuse.
  private scratchPath(): string {
    return `${this.options.path}.compact`
  }

  private async removeStaleScratch(): Promise<void> {
    try {
      await this.options.fs.unlink(this.scratchPath())
    } catch {
      // Missing scratch is the normal case; an unsafe path fails the
      // exclusive create below instead of being followed.
    }
  }

  async replace(operations: readonly TraceJournalOperation[]): Promise<number> {
    this.assertAvailable()
    const temporary = this.scratchPath()
    const encoded = Buffer.from(operations.map(operation => encodeOperationLine(operation)).join(''), 'utf8')

    await this.removeStaleScratch()
    let scratchExists = false

    try {
      await this.options.fs.writeFile(temporary, encoded, { exclusive: true })
      scratchExists = true
      await this.options.fs.syncFile(temporary)
      const journalExists = (await this.options.fs.stat(this.options.path)) !== null

      await (journalExists
        ? this.options.fs.replaceFile(temporary, this.options.path)
        : this.options.fs.rename(temporary, this.options.path))
      scratchExists = false
      await this.options.fs.syncDirectory(dirname(this.options.path))
    } finally {
      if (scratchExists) {
        await this.options.fs.unlink(temporary).catch(() => {})
      }
    }

    return encoded.length
  }

  async recover(): Promise<TraceJournalRecovery> {
    this.recoveredTornTailOffset = null
    await this.removeStaleScratch()
    const size = await this.options.fs.stat(this.options.path)

    if (size === null || size === 0) {
      return { operations: [], recoveredTornTail: false }
    }

    if (size > MAX_JOURNAL_BYTES) {
      throw new Error('trace_outbox_journal_too_large')
    }

    const operations: TraceJournalOperation[] = []
    let offset = 0
    let pending = Buffer.alloc(0)

    while (offset < size) {
      const length = Math.min(JOURNAL_CHUNK_BYTES, size - offset)
      const chunk = await this.options.fs.readRange(this.options.path, offset, length)

      if (chunk === null) {
        throw new Error('trace_outbox_journal_short_read')
      }
      offset += length
      pending = Buffer.concat([pending, chunk])
      let newline: number

      while ((newline = pending.indexOf(0x0a)) !== -1) {
        const line = pending.subarray(0, newline)

        if (line.length === 0) {
          throw new Error('invalid_journal_line')
        }

        if (line.length >= JOURNAL_CHUNK_BYTES) {
          throw new Error('trace_outbox_journal_line_too_large')
        }
        operations.push(parseLine(line))
        pending = pending.subarray(newline + 1)
      }

      // A partial line may span chunk boundaries, but a single encoded line
      // (including its newline) must never exceed one recovery chunk.
      if (pending.length >= JOURNAL_CHUNK_BYTES) {
        throw new Error('trace_outbox_journal_line_too_large')
      }
    }

    if (pending.length > 0) {
      this.recoveredTornTailOffset = size - pending.length

      return { operations, recoveredTornTail: true }
    }

    return { operations, recoveredTornTail: false }
  }

  async truncateRecoveredTornTail(): Promise<void> {
    if (this.recoveredTornTailOffset === null) {
      return
    }

    await this.options.fs.truncateFile(this.options.path, this.recoveredTornTailOffset)
    this.recoveredTornTailOffset = null
  }

  async sync(): Promise<void> {
    await this.options.fs.syncFile(this.options.path)
  }
}
