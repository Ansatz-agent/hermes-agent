import { createHash } from 'node:crypto'
import { constants, type Stats } from 'node:fs'
import { lstat, mkdir, open, rename, unlink } from 'node:fs/promises'
import { type FileHandle } from 'node:fs/promises'

export interface TraceFileSystem {
  appendFile(path: string, data: Buffer): Promise<void>
  mkdir(path: string): Promise<void>
  readFile(path: string): Promise<Buffer | null>
  readRange(path: string, offset: number, length: number): Promise<Buffer | null>
  rename(from: string, to: string): Promise<void>
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

export type TraceJournalOperation = TraceJournalPendingOperation | TraceJournalReceiptOperation

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
  outcome: 'accepted' | 'duplicate'
  receivedAt: number
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

  return (
    operation.op === 'receipt' &&
    typeof operation.batchId === 'string' &&
    (operation.outcome === 'accepted' || operation.outcome === 'duplicate') &&
    typeof operation.receivedAt === 'number' &&
    Number.isSafeInteger(operation.receivedAt) &&
    operation.receivedAt >= 0
  )
}

function isOperation(value: unknown): value is TraceJournalOperation {
  return isPendingOperation(value) || isReceiptOperation(value)
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

  private constructor(private readonly options: TraceJournalOptions) {}

  async append(operations: readonly TraceJournalOperation[]): Promise<void> {
    if (operations.length === 0) {
      return
    }

    const encoded = Buffer.from(
      operations
        .map(operation => canonicalJson({ checksum: operationChecksum(operation), operation }))
        .map(line => `${line}\n`)
        .join(''),
      'utf8'
    )

    await this.options.fs.appendFile(this.options.path, encoded)
  }

  async recover(): Promise<TraceJournalRecovery> {
    this.recoveredTornTailOffset = null
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

      if (pending.length > JOURNAL_CHUNK_BYTES) {
        throw new Error('trace_outbox_journal_line_too_large')
      }
      let newline: number

      while ((newline = pending.indexOf(0x0a)) !== -1) {
        const line = pending.subarray(0, newline)

        if (line.length === 0) {
          throw new Error('invalid_journal_line')
        }
        operations.push(parseLine(line))
        pending = pending.subarray(newline + 1)

        if (pending.length > JOURNAL_CHUNK_BYTES) {
          throw new Error('trace_outbox_journal_line_too_large')
        }
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
