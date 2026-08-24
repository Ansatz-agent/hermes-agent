import { createHash } from 'node:crypto'
import { appendFile, mkdir, open, readFile, rename, unlink, writeFile } from 'node:fs/promises'

export interface TraceFileSystem {
  appendFile(path: string, data: Buffer): Promise<void>
  mkdir(path: string): Promise<void>
  readFile(path: string): Promise<Buffer | null>
  rename(from: string, to: string): Promise<void>
  syncDirectory(path: string): Promise<void>
  syncFile(path: string): Promise<void>
  unlink(path: string): Promise<void>
  writeFile(path: string, data: Buffer, options?: { exclusive?: boolean }): Promise<void>
}

export const nodeTraceFileSystem: TraceFileSystem = {
  appendFile,
  async mkdir(path: string): Promise<void> {
    await mkdir(path, { recursive: true })
  },
  async readFile(path: string): Promise<Buffer | null> {
    try {
      return await readFile(path)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        return null
      }

      throw error
    }
  },
  rename,
  async syncDirectory(path: string): Promise<void> {
    const handle = await open(path, 'r')

    try {
      await handle.sync()
    } finally {
      await handle.close()
    }
  },
  async syncFile(path: string): Promise<void> {
    const handle = await open(path, 'r')

    try {
      await handle.sync()
    } finally {
      await handle.close()
    }
  },
  unlink,
  async writeFile(path: string, data: Buffer, options?: { exclusive?: boolean }): Promise<void> {
    await writeFile(path, data, { flag: options?.exclusive ? 'wx' : 'w' })
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
    const contents = await this.options.fs.readFile(this.options.path)

    if (contents === null || contents.length === 0) {
      return { operations: [], recoveredTornTail: false }
    }

    const operations: TraceJournalOperation[] = []
    let offset = 0

    while (offset < contents.length) {
      const newline = contents.indexOf(0x0a, offset)

      if (newline === -1) {
        return { operations, recoveredTornTail: true }
      }

      const line = contents.subarray(offset, newline)

      if (line.length === 0) {
        throw new Error('invalid_journal_line')
      }

      try {
        operations.push(parseLine(line))
      } catch (error) {
        if (newline === contents.length - 1) {
          throw error
        }

        throw error
      }

      offset = newline + 1
    }

    return { operations, recoveredTornTail: false }
  }

  async sync(): Promise<void> {
    await this.options.fs.syncFile(this.options.path)
  }
}
