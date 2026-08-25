import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { dirname, join } from 'node:path'

import { decryptTraceRecord, encryptTraceRecord, type TraceKeyProtector } from './trace-outbox-crypto'
import {
  nodeTraceFileSystem,
  type TraceFileSystem,
  TraceJournal,
  type TraceJournalOperation,
  traceJournalOperationBytes,
  type TraceJournalOwnerOperation,
  type TraceJournalPendingOperation,
  type TraceJournalReceiptOperation,
  type TraceJournalTerminalOperation
} from './trace-outbox-journal'
import {
  decodeSegmentRecord,
  encodeSegmentRecord,
  isValidTraceSegmentHeader,
  type TraceSegmentHeader
} from './trace-outbox-record'
import {
  type DurableReceipt,
  type DurableTraceBatch,
  type TraceEnvelopeInput,
  type TraceOutboxDiagnostics,
  type TraceOwner,
  validateTraceOwner
} from './trace-outbox-types'

const DEFAULT_GROUP_COMMIT_BYTES = 8 * 1024 * 1024
const DEFAULT_GROUP_COMMIT_MS = 50
const DEFAULT_CAPACITY_BYTES = 2 * 1024 * 1024 * 1024
const DEFAULT_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
const MAX_NORMAL_PAYLOAD_BYTES = 8 * 1024 * 1024
const ONE_GIB = 1024 * 1024 * 1024
const MAX_RECEIPT_BYTES = 64 * 1024 * 1024
const MAX_RECEIPT_ENTRIES = 100_000
const RECORD_PREFIX_BYTES = 25
const MAX_RECORD_BYTES = RECORD_PREFIX_BYTES + 64 * 1024 + 12 + 16 + 64 * 1024 * 1024 + 32
const KEY_FILE_NAME = 'key.json'
const SEGMENT_FILE_NAME = 'active.segment'
const JOURNAL_FILE_NAME = 'index.journal'

interface LegacyPersistedKey {
  version: 1
  wrappedKey: string
}

interface AccountBoundPersistedKey {
  accountKey: string
  version: 2
  wrappedKey: string
}

type PersistedKey = LegacyPersistedKey | AccountBoundPersistedKey

interface PendingCommit {
  batchId: string
  cancellation: DurableReceipt | null
  cancellationPromise: Promise<void> | null
  durable: Promise<DurableTraceBatch>
  input: TraceEnvelopeInput | null
  reject: (error: unknown) => void
  resolve: (batch: DurableTraceBatch) => void
  sequence: number
  state: 'queued' | 'flushing' | 'committed' | 'cancelled'
  receiptJournaled: boolean
}

interface PreparedRecord {
  batch: DurableTraceBatch
  encoded: Buffer
  item: PendingCommit
}

interface StoredRecord {
  batch: Omit<DurableTraceBatch, 'body'>
  encodedBytes: number
  offset: number
  state: 'pending' | 'quarantined' | 'receipt' | 'terminal'
}

interface ReceiptTombstone extends DurableReceipt {
  accountKey: string | null
  dedupeKey: string | null
}

export interface TraceFreeSpace {
  available: number
  total: number
}

export interface TraceOutboxStoreOptions {
  capacityBytes?: number
  freeSpace?: () => TraceFreeSpace | Promise<TraceFreeSpace>
  fs?: TraceFileSystem
  groupCommitBytes?: number
  groupCommitMs?: number
  isConversationStreaming?: () => boolean
  keyProtector: TraceKeyProtector
  maxBytes?: number
  monotonicNow?: () => number
  now?: () => number
  expectedOwner: TraceOwner
  receiptCapacityBytes?: number
  receiptCapacityEntries?: number
  retentionMs?: number
  root: string
}

export interface PendingLocalCommit {
  batchId: string
  cancelForGatewayReceipt(receipt: DurableReceipt): Promise<void>
  durable: Promise<DurableTraceBatch>
}

export type TraceOutboxStoreDiagnostics = TraceOutboxDiagnostics & { payloadBytes: number }

function sha256(input: Buffer): string {
  return createHash('sha256').update(input).digest('hex')
}

function keyJson(wrappedKey: Buffer, accountKey: string): Buffer {
  const encoded: AccountBoundPersistedKey = { accountKey, version: 2, wrappedKey: wrappedKey.toString('base64') }

  return Buffer.from(JSON.stringify(encoded), 'utf8')
}

function parseKey(contents: Buffer): PersistedKey {
  let parsed: unknown

  try {
    parsed = JSON.parse(contents.toString('utf8'))
  } catch {
    throw new Error('invalid_trace_outbox_key')
  }

  if (
    parsed === null ||
    typeof parsed !== 'object' ||
    Array.isArray(parsed) ||
    typeof (parsed as Record<string, unknown>).wrappedKey !== 'string'
  ) {
    throw new Error('invalid_trace_outbox_key')
  }

  const record = parsed as Record<string, unknown>
  const version = record.version
  const expectedKeys = version === 1 ? 'version,wrappedKey' : 'accountKey,version,wrappedKey'

  if (
    (version !== 1 && version !== 2) ||
    Object.keys(record).sort().join(',') !== expectedKeys ||
    (version === 2 && typeof record.accountKey !== 'string')
  ) {
    throw new Error('invalid_trace_outbox_key')
  }

  const wrappedKey = record.wrappedKey as string

  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(wrappedKey) || wrappedKey.length % 4 !== 0) {
    throw new Error('invalid_trace_outbox_key')
  }

  return version === 1 ? { version, wrappedKey } : { accountKey: record.accountKey as string, version, wrappedKey }
}

export class TraceOutboxStore {
  static async open(options: TraceOutboxStoreOptions): Promise<TraceOutboxStore> {
    const expectedOwner = validateTraceOwner(options.expectedOwner).owner
    const fs = options.fs ?? nodeTraceFileSystem
    const groupCommitBytes = options.groupCommitBytes ?? DEFAULT_GROUP_COMMIT_BYTES
    const groupCommitMs = options.groupCommitMs ?? DEFAULT_GROUP_COMMIT_MS
    const capacityBytes = options.maxBytes ?? options.capacityBytes ?? DEFAULT_CAPACITY_BYTES
    const receiptCapacityBytes = options.receiptCapacityBytes ?? MAX_RECEIPT_BYTES
    const receiptCapacityEntries = options.receiptCapacityEntries ?? MAX_RECEIPT_ENTRIES
    const retentionMs = options.retentionMs ?? DEFAULT_RETENTION_MS

    if (!Number.isSafeInteger(groupCommitBytes) || groupCommitBytes <= 0) {
      throw new RangeError('invalid_group_commit_bytes')
    }

    if (!Number.isSafeInteger(groupCommitMs) || groupCommitMs < 0 || groupCommitMs > DEFAULT_GROUP_COMMIT_MS) {
      throw new RangeError('invalid_group_commit_ms')
    }

    if (!Number.isSafeInteger(capacityBytes) || capacityBytes <= 0) {
      throw new RangeError('invalid_trace_outbox_capacity')
    }

    if (!Number.isSafeInteger(retentionMs) || retentionMs < 0) {
      throw new RangeError('invalid_trace_outbox_retention')
    }

    if (!Number.isSafeInteger(receiptCapacityBytes) || receiptCapacityBytes <= 0) {
      throw new RangeError('invalid_trace_receipt_capacity_bytes')
    }

    if (!Number.isSafeInteger(receiptCapacityEntries) || receiptCapacityEntries <= 0) {
      throw new RangeError('invalid_trace_receipt_capacity_entries')
    }

    await fs.mkdir(options.root)
    const segmentDirectory = join(options.root, 'segments')
    await fs.mkdir(segmentDirectory)
    const keyPath = join(options.root, KEY_FILE_NAME)
    let dataKey: Buffer
    let legacyUnboundKey = false
    let keyLost = false

    try {
      const loaded = await TraceOutboxStore.loadOrCreateKey(
        fs,
        keyPath,
        options.root,
        options.keyProtector,
        expectedOwner.accountKey
      )
      dataKey = loaded.dataKey
      legacyUnboundKey = loaded.legacyUnbound
    } catch (error) {
      if ((error as Error).message === 'trace_outbox_account_mismatch') {
        throw error
      }

      if ((await fs.readFile(keyPath)) === null) {
        throw error
      }

      dataKey = randomBytes(32)
      keyLost = true
    }
    const journal = await TraceJournal.open({ fs, path: join(options.root, JOURNAL_FILE_NAME) })

    const store = new TraceOutboxStore({
      dataKey,
      capacityBytes,
      expectedAccountKey: expectedOwner.accountKey,
      freeSpace: options.freeSpace ?? (() => fs.freeSpace(options.root)),
      fs,
      groupCommitBytes,
      groupCommitMs,
      isConversationStreaming: options.isConversationStreaming ?? (() => false),
      journal,
      keyLost,
      legacyUnboundKey,
      monotonicNow: options.monotonicNow ?? performance.now.bind(performance),
      now: options.now ?? Date.now,
      receiptCapacityBytes,
      receiptCapacityEntries,
      retentionMs,
      segmentPath: join(segmentDirectory, SEGMENT_FILE_NAME)
    })

    await store.recover()

    if (keyLost) {
      store.quarantineForKeyLoss()
    } else if (legacyUnboundKey && store.accountKey === expectedOwner.accountKey && !store.legacyOwnerUnknown) {
      await TraceOutboxStore.persistBoundKey(
        fs,
        keyPath,
        options.root,
        options.keyProtector,
        dataKey,
        expectedOwner.accountKey,
        true
      )
    }

    return store
  }

  private static async loadOrCreateKey(
    fs: TraceFileSystem,
    keyPath: string,
    root: string,
    keyProtector: TraceKeyProtector,
    expectedAccountKey: string
  ): Promise<{ dataKey: Buffer; legacyUnbound: boolean }> {
    if (!keyProtector.available()) {
      throw new Error('secure_key_storage_unavailable')
    }

    const existing = await fs.readFile(keyPath)

    if (existing !== null) {
      const persisted = parseKey(existing)

      if (persisted.version === 2) {
        if (persisted.accountKey !== expectedAccountKey) {
          throw new Error('trace_outbox_account_mismatch')
        }

        return {
          dataKey: keyProtector.unwrap(Buffer.from(persisted.wrappedKey, 'base64'), expectedAccountKey),
          legacyUnbound: false
        }
      }

      return {
        dataKey: keyProtector.unwrap(Buffer.from(persisted.wrappedKey, 'base64')),
        legacyUnbound: true
      }
    }

    const dataKey = randomBytes(32)
    await TraceOutboxStore.persistBoundKey(fs, keyPath, root, keyProtector, dataKey, expectedAccountKey, false)

    return { dataKey, legacyUnbound: false }
  }

  private static async persistBoundKey(
    fs: TraceFileSystem,
    keyPath: string,
    root: string,
    keyProtector: TraceKeyProtector,
    dataKey: Buffer,
    accountKey: string,
    replace: boolean
  ): Promise<void> {
    const temporaryPath = `${keyPath}.tmp-${randomUUID()}`
    await fs.writeFile(temporaryPath, keyJson(keyProtector.wrap(dataKey, accountKey), accountKey), { exclusive: true })
    await fs.syncFile(temporaryPath)
    await (replace ? fs.replaceFile(temporaryPath, keyPath) : fs.rename(temporaryPath, keyPath))
    await fs.syncDirectory(root)
  }

  private readonly pending: PendingCommit[] = []
  private readonly dedupe = new Map<string, string>()
  private readonly receipts = new Map<string, ReceiptTombstone>()
  private readonly records = new Map<string, StoredRecord>()
  private admissionClosed = false
  private closePromise: Promise<void> | null = null
  private flushPromise: Promise<void> | null = null
  private flushTimer: ReturnType<typeof setTimeout> | null = null
  private journalTail: Promise<void> = Promise.resolve()
  private writerTail: Promise<void> = Promise.resolve()
  private nextSequence = 0
  private pendingInputBytes = 0
  private pendingDeadline: number | null = null
  private segmentOffset = 0
  private segmentWritable = true
  private quarantinedSegments = 0
  private recoveredCorruptTail = 0
  private accepted = 0
  private deduplicated = 0
  private evictedCapacity = 0
  private expired = 0
  private accountKey: string | null
  private ownerJournaled = false
  private legacyOwnerUnknown = false

  private constructor(
    private readonly config: {
      dataKey: Buffer
      capacityBytes: number
      expectedAccountKey: string
      freeSpace: () => TraceFreeSpace | Promise<TraceFreeSpace>
      fs: TraceFileSystem
      groupCommitBytes: number
      groupCommitMs: number
      isConversationStreaming: () => boolean
      journal: TraceJournal
      keyLost: boolean
      legacyUnboundKey: boolean
      monotonicNow: () => number
      now: () => number
      receiptCapacityBytes: number
      receiptCapacityEntries: number
      retentionMs: number
      segmentPath: string
    }
  ) {
    this.accountKey = config.legacyUnboundKey ? null : config.expectedAccountKey
  }

  beginEnqueue(input: TraceEnvelopeInput): PendingLocalCommit {
    return this.beginEnqueueInternal(input, false)
  }

  private beginEnqueueInternal(input: TraceEnvelopeInput, bypassNormalAdmission: boolean): PendingLocalCommit {
    if (this.admissionClosed) {
      throw new Error('trace_outbox_closed')
    }

    validateTraceOwner(input.owner)

    if (this.legacyOwnerUnknown) {
      throw new Error('trace_outbox_account_unknown')
    }

    if (this.accountKey === null) {
      this.accountKey = input.owner.accountKey
    } else if (this.accountKey !== input.owner.accountKey) {
      throw new Error('trace_outbox_account_mismatch')
    }

    if (!Buffer.isBuffer(input.body)) {
      throw new TypeError('invalid_trace_payload')
    }

    if (!bypassNormalAdmission && input.body.length > MAX_NORMAL_PAYLOAD_BYTES) {
      throw new RangeError('payload_too_large')
    }

    const dedupeKey = this.dedupeKey(input)
    const existingBatchId = this.dedupe.get(dedupeKey)

    if (existingBatchId !== undefined) {
      const existingPending = this.pending.find(item => item.batchId === existingBatchId)

      this.deduplicated += 1

      if (existingPending !== undefined) {
        return {
          batchId: existingBatchId,
          cancelForGatewayReceipt: receipt => this.cancelForGatewayReceipt(existingPending, receipt),
          durable: existingPending.durable
        }
      }

      const existingReceipt = this.receipts.get(existingBatchId)

      if (existingReceipt !== undefined && existingReceipt.dedupeKey === dedupeKey) {
        return {
          batchId: existingBatchId,
          cancelForGatewayReceipt: receipt => this.validateDuplicateReceipt(existingReceipt, receipt),
          durable: Promise.resolve(this.receiptSafeBatch(input, existingReceipt))
        }
      }

      return {
        batchId: existingBatchId,
        cancelForGatewayReceipt: receipt => this.acknowledge(existingBatchId, receipt),
        durable: this.readStoredBatch(existingBatchId)
      }
    }

    const batchId = randomUUID()
    let resolve!: (batch: DurableTraceBatch) => void
    let reject!: (error: unknown) => void

    const durable = new Promise<DurableTraceBatch>((currentResolve, currentReject) => {
      resolve = currentResolve
      reject = currentReject
    })

    const item: PendingCommit = {
      batchId,
      cancellation: null,
      cancellationPromise: null,
      durable,
      input: { ...input, body: Buffer.from(input.body), owner: { ...input.owner } },
      receiptJournaled: false,
      reject,
      resolve,
      sequence: this.nextSequence++,
      state: 'queued'
    }

    this.pending.push(item)
    this.dedupe.set(dedupeKey, batchId)
    this.pendingInputBytes += input.body.length

    if (this.pendingDeadline === null) {
      this.pendingDeadline = this.config.monotonicNow() + this.config.groupCommitMs
    }

    this.scheduleFlush()

    return {
      batchId,
      durable,
      cancelForGatewayReceipt: receipt => this.cancelForGatewayReceipt(item, receipt)
    }
  }

  async enqueue(input: TraceEnvelopeInput): Promise<DurableTraceBatch> {
    return this.beginEnqueue(input).durable
  }

  close(): Promise<void> {
    if (this.closePromise !== null) {
      return this.closePromise
    }

    this.admissionClosed = true
    const closing = this.drainForClose()
    this.closePromise = closing

    return closing
  }

  async quarantineInput(input: TraceEnvelopeInput, errorClass: string): Promise<DurableTraceBatch> {
    const pending = this.beginEnqueueInternal(input, true)
    const batch = await pending.durable
    await this.quarantine(batch.batchId, errorClass)

    return batch
  }

  async diagnostics(): Promise<TraceOutboxStoreDiagnostics> {
    if (this.pruneReceipts(this.config.now())) {
      await this.compactIfIdle()
    }
    let payloadBytes = 0
    let pending = 0
    let quarantined = this.quarantinedSegments

    for (const record of this.records.values()) {
      if (record.state === 'quarantined') {
        quarantined += 1
      }

      if (record.state !== 'pending' && record.state !== 'quarantined') {
        continue
      }

      payloadBytes += record.encodedBytes
      pending += 1
    }

    return {
      accepted: this.accepted,
      deduplicated: this.deduplicated,
      duplicate: this.deduplicated,
      evictedCapacity: this.evictedCapacity,
      expired: this.expired,
      keyLost: Number(this.config.keyLost),
      payloadBytes,
      pending,
      pendingBytes: payloadBytes,
      quarantined,
      recoveredCorruptTail: this.recoveredCorruptTail,
      tombstoneBytes: this.receiptBytes(),
      tombstones: this.receipts.size
    }
  }

  async lookupReceipt(batchId: string): Promise<DurableReceipt | undefined> {
    if (this.pruneReceipts(this.config.now())) {
      await this.compactIfIdle()
    }
    const receipt = this.receipts.get(batchId)

    if (receipt === undefined) {
      return undefined
    }

    return {
      batchId: receipt.batchId,
      outcome: receipt.outcome,
      receivedAt: receipt.receivedAt
    }
  }

  async peekEligible(now: number): Promise<DurableTraceBatch | undefined> {
    if (!Number.isSafeInteger(now) || now < 0) {
      throw new RangeError('invalid_trace_outbox_peek_time')
    }

    const head = [...this.records.values()]
      .filter(record => record.state === 'pending')
      .sort((left, right) => left.batch.sequence - right.batch.sequence)[0]

    if (head === undefined || head.batch.nextRetryAt > now) {
      return undefined
    }

    try {
      const size = await this.config.fs.stat(this.config.segmentPath)

      if (size === null || head.offset + head.encodedBytes > size || head.encodedBytes > MAX_RECORD_BYTES) {
        throw new Error('missing_trace_outbox_segment')
      }

      const segment = await this.config.fs.readRange(this.config.segmentPath, head.offset, head.encodedBytes)

      if (segment === null) {
        throw new Error('trace_outbox_segment_short_read')
      }

      const decoded = decodeSegmentRecord(segment, 0)

      if (
        decoded === null ||
        decoded.header.batchId !== head.batch.batchId ||
        decoded.header.sequence !== head.batch.sequence ||
        decoded.header.owner.accountKey !== head.batch.owner.accountKey
      ) {
        throw new Error('invalid_trace_outbox_segment_index')
      }

      const body = await decryptTraceRecord(
        decoded.encrypted,
        this.config.dataKey,
        Buffer.from(`${decoded.header.owner.accountKey}/${decoded.header.batchId}`, 'utf8')
      )

      if (sha256(body) !== decoded.header.payloadSha256) {
        throw new Error('invalid_trace_outbox_payload_digest')
      }

      return { ...decoded.header, body }
    } catch {
      head.state = 'quarantined'
      this.quarantinedSegments = Math.max(this.quarantinedSegments, 1)

      return undefined
    }
  }

  async acknowledge(batchId: string, receipt: DurableReceipt): Promise<void> {
    this.validateReceipt(batchId, receipt)
    const record = this.records.get(batchId)
    const existing = this.receipts.get(batchId)

    if (record === undefined && existing === undefined) {
      throw new Error('unknown_trace_batch')
    }

    await this.persistReceipt(
      receipt,
      record === undefined
        ? existing!
        : { accountKey: record.batch.owner.accountKey, dedupeKey: this.dedupeKey(record.batch) }
    )
    await this.compactIfIdle()
  }

  async quarantine(batchId: string, errorClass: string): Promise<void> {
    if (!/^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$/.test(errorClass)) {
      throw new TypeError('invalid_trace_error_class')
    }

    const record = this.records.get(batchId)

    if (record === undefined) {
      throw new Error('unknown_trace_batch')
    }

    if (record.state === 'receipt' || record.state === 'terminal') {
      return
    }

    await this.recordTerminal(record, { errorClass, terminal: 'quarantined' })
  }

  async compactIfIdle(): Promise<boolean> {
    if (this.flushPromise !== null || this.pending.length !== 0) {
      return false
    }

    return this.withWriterLock(() => this.compactForCapacity())
  }

  private async compactForCapacity(): Promise<boolean> {
    const reclaimed = await this.reclaimFullyTerminalSegments()
    const compacted = reclaimed || (!this.config.isConversationStreaming() && (await this.compactSegmentIfNeeded()))
    await this.compactJournal()

    return compacted
  }

  private async recover(): Promise<void> {
    const scanned = await this.scanActiveSegment()
    const recovered = await this.config.journal.recover()
    const pendingOperations = new Set<string>()
    const terminalStates = new Map<string, TraceJournalTerminalOperation>()

    for (const operation of recovered.operations) {
      if (operation.op === 'owner') {
        if (operation.accountKey === null) {
          if (this.accountKey !== null) {
            throw new Error('trace_outbox_account_mismatch')
          }
          this.legacyOwnerUnknown = true
        } else {
          if (this.legacyOwnerUnknown) {
            throw new Error('trace_outbox_account_mismatch')
          }
          this.bindRecoveredAccount(operation.accountKey)
        }
        this.ownerJournaled = true
      }

      if (operation.op === 'receipt') {
        const tombstone: ReceiptTombstone = {
          accountKey: operation.accountKey ?? null,
          batchId: operation.batchId,
          dedupeKey: operation.dedupeKey ?? null,
          outcome: operation.outcome,
          receivedAt: operation.receivedAt
        }
        const prior = this.receipts.get(operation.batchId)
        if (
          prior !== undefined &&
          (prior.outcome !== tombstone.outcome ||
            prior.receivedAt !== tombstone.receivedAt ||
            (prior.accountKey !== null && tombstone.accountKey !== null && prior.accountKey !== tombstone.accountKey) ||
            (prior.dedupeKey !== null && tombstone.dedupeKey !== null && prior.dedupeKey !== tombstone.dedupeKey))
        ) {
          throw new Error('conflicting_gateway_receipt')
        }
        if (prior !== undefined) {
          tombstone.accountKey ??= prior.accountKey
          tombstone.dedupeKey ??= prior.dedupeKey
        }
        if (tombstone.accountKey !== null) {
          this.bindRecoveredAccount(tombstone.accountKey)
        }
        this.receipts.set(operation.batchId, tombstone)
        if (tombstone.dedupeKey !== null) {
          this.dedupe.set(tombstone.dedupeKey, tombstone.batchId)
        }
        terminalStates.delete(operation.batchId)
      }

      if (operation.op === 'pending') {
        pendingOperations.add(operation.batchId)
        this.nextSequence = Math.max(this.nextSequence, operation.sequence + 1)
      }

      if (operation.op === 'terminal') {
        terminalStates.set(operation.batchId, operation)
        const supersededReceipt = this.receipts.get(operation.batchId)
        this.receipts.delete(operation.batchId)
        if (
          supersededReceipt?.dedupeKey !== null &&
          supersededReceipt?.dedupeKey !== undefined &&
          this.dedupe.get(supersededReceipt.dedupeKey) === operation.batchId
        ) {
          this.dedupe.delete(supersededReceipt.dedupeKey)
        }
      }
    }

    this.evictedCapacity = [...terminalStates.values()].filter(operation => operation.terminal === 'evicted').length

    for (const record of scanned.records) {
      this.bindRecoveredAccount(record.header.owner.accountKey)
      this.nextSequence = Math.max(this.nextSequence, record.header.sequence + 1)
      const receipt = this.receipts.get(record.header.batchId)
      const terminal = terminalStates.get(record.header.batchId)
      const dedupeKey = this.dedupeKey(record.header)

      if (receipt !== undefined) {
        if (receipt.accountKey !== null && receipt.accountKey !== record.header.owner.accountKey) {
          throw new Error('trace_outbox_account_mismatch')
        }
        if (receipt.dedupeKey !== null && receipt.dedupeKey !== dedupeKey) {
          throw new Error('invalid_trace_receipt_dedupe')
        }
        receipt.accountKey = record.header.owner.accountKey
        receipt.dedupeKey = dedupeKey
      }

      this.records.set(record.header.batchId, {
        batch: {
          ...record.header,
          lastErrorClass: terminal?.terminal === 'quarantined' ? terminal.errorClass : record.header.lastErrorClass
        },
        encodedBytes: record.encodedBytes,
        offset: record.offset,
        state:
          terminal?.terminal === 'quarantined'
            ? 'quarantined'
            : terminal?.terminal === 'evicted'
              ? 'terminal'
              : receipt === undefined
                ? 'pending'
                : 'receipt'
      })
      this.dedupe.set(dedupeKey, record.header.batchId)
    }

    this.segmentOffset = scanned.nextOffset
    this.segmentWritable = !scanned.quarantined
    this.quarantinedSegments = scanned.quarantined ? 1 : 0
    this.recoveredCorruptTail = Number(scanned.recoveredTornTail || recovered.recoveredTornTail)

    if (scanned.recoveredTornTail) {
      await this.config.fs.truncateFile(this.config.segmentPath, scanned.nextOffset)
      await this.config.fs.syncFile(this.config.segmentPath)
    }

    if (recovered.recoveredTornTail) {
      await this.config.journal.truncateRecoveredTornTail()
      await this.config.journal.sync()
    }

    const reconstructed = scanned.records
      .filter(record => !pendingOperations.has(record.header.batchId) && !this.receipts.has(record.header.batchId))
      .map(record => this.pendingOperationFrom(record))

    if (reconstructed.length > 0) {
      await this.appendAndSyncJournal(this.withOwnerOperation(reconstructed))
      this.ownerJournaled = this.accountKey !== null
    }

    this.legacyOwnerUnknown ||=
      this.accountKey === null &&
      [...this.receipts.values()].some(receipt => receipt.accountKey === null || receipt.dedupeKey === null)

    if (this.pruneReceipts(this.config.now())) {
      await this.compactJournal()
    }
  }

  private async scanActiveSegment(): Promise<{
    nextOffset: number
    quarantined: boolean
    records: Array<{ encodedBytes: number; header: TraceSegmentHeader; offset: number }>
    recoveredTornTail: boolean
    trustedPrefix: Buffer
  }> {
    const size = await this.config.fs.stat(this.config.segmentPath)

    if (size === null || size === 0) {
      return {
        nextOffset: 0,
        quarantined: false,
        records: [],
        recoveredTornTail: false,
        trustedPrefix: Buffer.alloc(0)
      }
    }

    if (size > this.config.capacityBytes) {
      return { nextOffset: 0, quarantined: true, records: [], recoveredTornTail: false, trustedPrefix: Buffer.alloc(0) }
    }

    const records: Array<{ encodedBytes: number; header: TraceSegmentHeader; offset: number }> = []
    let offset = 0

    while (offset < size) {
      try {
        if (size - offset < RECORD_PREFIX_BYTES) {
          return {
            nextOffset: offset,
            quarantined: false,
            records,
            recoveredTornTail: true,
            trustedPrefix: Buffer.alloc(0)
          }
        }

        const prefix = await this.config.fs.readRange(this.config.segmentPath, offset, RECORD_PREFIX_BYTES)

        if (prefix === null) {
          return {
            nextOffset: offset,
            quarantined: true,
            records: [],
            recoveredTornTail: false,
            trustedPrefix: Buffer.alloc(0)
          }
        }

        const headerLength = prefix.readUInt32BE(5)
        const nonceLength = prefix.readUInt32BE(9)
        const tagLength = prefix.readUInt32BE(13)
        const ciphertextLength = prefix.readUInt32BE(17)
        if (
          !prefix.subarray(0, 4).equals(Buffer.from('ATOB')) ||
          prefix.readUInt8(4) !== 1 ||
          nonceLength !== 12 ||
          tagLength !== 16 ||
          headerLength > 64 * 1024 ||
          ciphertextLength > 64 * 1024 * 1024
        ) {
          return {
            nextOffset: offset,
            quarantined: true,
            records: [],
            recoveredTornTail: false,
            trustedPrefix: Buffer.alloc(0)
          }
        }
        const recordLength = RECORD_PREFIX_BYTES + headerLength + nonceLength + tagLength + ciphertextLength + 32
        if (recordLength > MAX_RECORD_BYTES) {
          return {
            nextOffset: offset,
            quarantined: true,
            records: [],
            recoveredTornTail: false,
            trustedPrefix: Buffer.alloc(0)
          }
        }
        if (recordLength > size - offset) {
          return {
            nextOffset: offset,
            quarantined: false,
            records,
            recoveredTornTail: true,
            trustedPrefix: Buffer.alloc(0)
          }
        }

        const encoded = await this.config.fs.readRange(this.config.segmentPath, offset, recordLength)

        if (encoded === null) {
          return {
            nextOffset: offset,
            quarantined: true,
            records: [],
            recoveredTornTail: false,
            trustedPrefix: Buffer.alloc(0)
          }
        }

        const decoded = decodeSegmentRecord(encoded, 0)

        if (decoded === null) {
          return {
            nextOffset: offset,
            quarantined: false,
            records,
            recoveredTornTail: true,
            trustedPrefix: Buffer.alloc(0)
          }
        }

        records.push({ encodedBytes: decoded.nextOffset, header: decoded.header, offset })
        offset += decoded.nextOffset
      } catch {
        return {
          nextOffset: offset,
          quarantined: true,
          records: [],
          recoveredTornTail: false,
          trustedPrefix: Buffer.alloc(0)
        }
      }
    }

    return {
      nextOffset: offset,
      quarantined: false,
      records,
      recoveredTornTail: false,
      trustedPrefix: Buffer.alloc(0)
    }
  }

  private pendingOperationFrom(record: {
    encodedBytes: number
    header: TraceSegmentHeader
    offset: number
  }): TraceJournalOperation {
    return {
      op: 'pending',
      batchId: record.header.batchId,
      createdAt: record.header.createdAt,
      length: record.encodedBytes,
      offset: record.offset,
      segment: SEGMENT_FILE_NAME,
      sequence: record.header.sequence
    }
  }

  private scheduleFlush(): void {
    if (this.admissionClosed || this.pending.length === 0 || this.flushPromise !== null) {
      return
    }

    const delay = Math.max(0, (this.pendingDeadline ?? this.config.monotonicNow()) - this.config.monotonicNow())

    if (this.pendingInputBytes >= this.config.groupCommitBytes || delay === 0) {
      void this.flush().catch(() => undefined)

      return
    }

    if (this.flushTimer === null) {
      this.flushTimer = setTimeout(() => {
        this.flushTimer = null
        void this.flush().catch(() => undefined)
      }, delay)
    }
  }

  private async flush(): Promise<void> {
    if (this.flushPromise !== null) {
      return this.flushPromise
    }

    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }

    const group = this.pending.splice(0)

    if (group.length === 0) {
      return
    }

    this.pendingInputBytes = 0
    this.pendingDeadline = null
    group.forEach(item => {
      item.state = 'flushing'
    })

    const currentFlush = this.withWriterLock(() => this.flushGroup(group))
    this.flushPromise = currentFlush

    try {
      await currentFlush
    } finally {
      if (this.flushPromise === currentFlush) {
        this.flushPromise = null
      }

      this.scheduleFlush()
    }
  }

  private async drainForClose(): Promise<void> {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }

    while (this.flushPromise !== null || this.pending.length > 0) {
      if (this.flushPromise !== null) {
        await this.flushPromise
      } else {
        await this.flush()
      }
    }

    await this.writerTail
    await this.journalTail
  }

  private async flushGroup(group: PendingCommit[]): Promise<void> {
    try {
      if (!this.segmentWritable) {
        throw new Error('trace_outbox_segment_quarantined')
      }

      const records = await Promise.all(group.map(item => this.prepareRecord(item)))
      await this.ensureCapacity(
        records.reduce((total, record) => total + record.encoded.length, 0),
        this.config.now()
      )
      let offset = this.segmentOffset
      const pendingOperations: TraceJournalPendingOperation[] = []

      for (const record of records) {
        await this.config.fs.appendFile(this.config.segmentPath, record.encoded)
        pendingOperations.push({
          op: 'pending',
          batchId: record.batch.batchId,
          createdAt: record.batch.createdAt,
          length: record.encoded.length,
          offset,
          segment: SEGMENT_FILE_NAME,
          sequence: record.batch.sequence
        })
        offset += record.encoded.length
      }

      await this.config.fs.syncFile(this.config.segmentPath)
      this.segmentOffset = offset

      const receiptOperations: TraceJournalReceiptOperation[] = group.flatMap((item, index) => {
        if (item.cancellation === null) {
          return []
        }

        const batch = records[index].batch

        return [
          {
            op: 'receipt',
            ...item.cancellation,
            accountKey: batch.owner.accountKey,
            dedupeKey: this.dedupeKey(batch)
          }
        ]
      })

      await this.appendAndSyncJournal(this.withOwnerOperation([...pendingOperations, ...receiptOperations]))
      this.ownerJournaled = this.accountKey !== null

      for (let index = 0; index < records.length; index += 1) {
        const record = records[index]
        const item = group[index]
        const { body: _body, ...metadata } = record.batch
        const receipt = item.cancellation

        item.input = null
        item.state = 'committed'
        item.receiptJournaled = receipt !== null
        this.records.set(record.batch.batchId, {
          batch: metadata,
          encodedBytes: record.encoded.length,
          offset: pendingOperations[index].offset,
          state: receipt === null ? 'pending' : 'receipt'
        })

        if (receipt !== null) {
          const dedupeKey = this.dedupeKey(record.batch)
          this.receipts.set(receipt.batchId, {
            ...receipt,
            accountKey: record.batch.owner.accountKey,
            dedupeKey
          })
          this.dedupe.set(dedupeKey, receipt.batchId)
        }

        this.accepted += 1
        item.resolve(record.batch)
      }
      this.pruneReceipts(this.config.now())
    } catch (error) {
      for (const item of group) {
        item.input = null
        item.state = 'cancelled'
        item.reject(error)
      }

      throw error
    }
  }

  private async prepareRecord(item: PendingCommit): Promise<PreparedRecord> {
    if (item.input === null) {
      throw new Error('local_commit_cancelled')
    }

    const owner = validateTraceOwner(item.input.owner).owner
    const body = Buffer.from(item.input.body)

    const batch: DurableTraceBatch = {
      ...item.input,
      attempt: 0,
      batchId: item.batchId,
      body,
      createdAt: this.config.now(),
      lastErrorClass: null,
      nextRetryAt: 0,
      owner,
      payloadSha256: sha256(body),
      sequence: item.sequence
    }

    const header = this.headerFrom(batch)

    if (!isValidTraceSegmentHeader(header)) {
      throw new Error('invalid_record_header')
    }

    const encrypted = await encryptTraceRecord(
      body,
      this.config.dataKey,
      Buffer.from(`${owner.accountKey}/${batch.batchId}`, 'utf8')
    )

    return { batch, encoded: encodeSegmentRecord({ encrypted, header }), item }
  }

  private headerFrom(batch: DurableTraceBatch): Omit<DurableTraceBatch, 'body'> {
    const { body: _body, ...header } = batch

    return header
  }

  private cancelForGatewayReceipt(item: PendingCommit, receipt: DurableReceipt): Promise<void> {
    try {
      this.validateReceipt(item.batchId, receipt)
    } catch (error) {
      return Promise.reject(error)
    }

    if (item.cancellation !== null) {
      if (item.cancellation.outcome !== receipt.outcome || item.cancellation.receivedAt !== receipt.receivedAt) {
        return Promise.reject(new Error('conflicting_gateway_receipt'))
      }

      return item.cancellationPromise ?? Promise.resolve()
    }

    item.cancellation = { ...receipt }
    const cancellation = this.persistCancellation(item, receipt)
    item.cancellationPromise = cancellation

    return cancellation
  }

  private async persistCancellation(item: PendingCommit, receipt: DurableReceipt): Promise<void> {
    if (item.state === 'queued') {
      const index = this.pending.indexOf(item)

      if (index !== -1) {
        this.pending.splice(index, 1)
      }

      const input = item.input
      this.pendingInputBytes -= input?.body.length ?? 0
      item.input = null
      item.state = 'cancelled'

      try {
        if (input === null) {
          throw new Error('local_commit_cancelled')
        }
        await this.persistReceipt(receipt, {
          accountKey: input.owner.accountKey,
          dedupeKey: this.dedupeKey(input)
        })
        await this.compactIfIdle()
        item.reject(new Error('local_commit_cancelled'))
      } catch (error) {
        item.reject(error)
        throw error
      }

      return
    }

    if (item.state === 'flushing') {
      await this.flushPromise

      if (item.receiptJournaled) {
        await this.compactIfIdle()
        return
      }
    }

    if (item.state === 'committed' || item.state === 'flushing') {
      const record = this.records.get(item.batchId)
      if (record === undefined) {
        throw new Error('unknown_trace_batch')
      }
      await this.persistReceipt(receipt, {
        accountKey: record.batch.owner.accountKey,
        dedupeKey: this.dedupeKey(record.batch)
      })
      await this.compactIfIdle()
    }
  }

  private async persistReceipt(
    receipt: DurableReceipt,
    identity: { accountKey: string | null; dedupeKey: string | null }
  ): Promise<void> {
    const existing = this.receipts.get(receipt.batchId)

    if (existing !== undefined) {
      if (existing.outcome !== receipt.outcome || existing.receivedAt !== receipt.receivedAt) {
        throw new Error('conflicting_gateway_receipt')
      }

      if (
        (existing.accountKey !== null && identity.accountKey !== null && existing.accountKey !== identity.accountKey) ||
        (existing.dedupeKey !== null && identity.dedupeKey !== null && existing.dedupeKey !== identity.dedupeKey)
      ) {
        throw new Error('conflicting_trace_receipt_identity')
      }

      if (
        identity.accountKey !== null &&
        identity.dedupeKey !== null &&
        (existing.accountKey === null || existing.dedupeKey === null)
      ) {
        await this.appendExtendedReceipt(receipt, identity.accountKey, identity.dedupeKey)
        existing.accountKey = identity.accountKey
        existing.dedupeKey = identity.dedupeKey
        this.dedupe.set(identity.dedupeKey, receipt.batchId)
      }

      return
    }

    if (identity.accountKey === null || identity.dedupeKey === null) {
      throw new Error('invalid_trace_receipt_identity')
    }

    await this.appendExtendedReceipt(receipt, identity.accountKey, identity.dedupeKey)
    this.receipts.set(receipt.batchId, { ...receipt, ...identity })
    this.dedupe.set(identity.dedupeKey, receipt.batchId)
    const record = this.records.get(receipt.batchId)

    if (record !== undefined) {
      record.state = 'receipt'
    }
    this.pruneReceipts(this.config.now())
  }

  private async appendExtendedReceipt(receipt: DurableReceipt, accountKey: string, dedupeKey: string): Promise<void> {
    await this.appendAndSyncJournal(this.withOwnerOperation([{ op: 'receipt', ...receipt, accountKey, dedupeKey }]))
    this.ownerJournaled = true
  }

  private validateReceipt(batchId: string, receipt: DurableReceipt): void {
    if (
      receipt.batchId !== batchId ||
      (receipt.outcome !== 'accepted' && receipt.outcome !== 'duplicate') ||
      !Number.isSafeInteger(receipt.receivedAt) ||
      receipt.receivedAt < 0
    ) {
      throw new TypeError('invalid_trace_receipt')
    }
  }

  private validateDuplicateReceipt(existing: ReceiptTombstone, receipt: DurableReceipt): Promise<void> {
    try {
      this.validateReceipt(existing.batchId, receipt)
      return Promise.resolve()
    } catch (error) {
      return Promise.reject(error)
    }
  }

  private receiptSafeBatch(input: TraceEnvelopeInput, receipt: ReceiptTombstone): DurableTraceBatch {
    return {
      ...input,
      attempt: 0,
      batchId: receipt.batchId,
      body: Buffer.from(input.body),
      createdAt: receipt.receivedAt,
      lastErrorClass: null,
      nextRetryAt: 0,
      owner: { ...input.owner },
      payloadSha256: sha256(input.body),
      sequence: 0
    }
  }

  private bindRecoveredAccount(accountKey: string): void {
    if (this.legacyOwnerUnknown) {
      throw new Error('trace_outbox_account_mismatch')
    }

    if (accountKey !== this.config.expectedAccountKey) {
      throw new Error('trace_outbox_account_mismatch')
    }

    if (this.accountKey === null) {
      this.accountKey = accountKey
    } else if (this.accountKey !== accountKey) {
      throw new Error('trace_outbox_account_mismatch')
    }
  }

  private withOwnerOperation(operations: TraceJournalOperation[]): TraceJournalOperation[] {
    if (this.ownerJournaled || (this.accountKey === null && !this.legacyOwnerUnknown)) {
      return operations
    }

    const owner: TraceJournalOwnerOperation = { op: 'owner', accountKey: this.accountKey }

    return [owner, ...operations]
  }

  private dedupeKey(
    input: Pick<TraceEnvelopeInput, 'entrypoint' | 'hermesSessionId' | 'owner' | 'runId'> & {
      payloadSha256?: string
      body?: Buffer
    }
  ): string {
    const payloadSha256 = input.payloadSha256 ?? sha256(input.body ?? Buffer.alloc(0))

    return sha256(
      Buffer.from(
        `${input.owner.accountKey}\u0000${input.entrypoint}\u0000${input.hermesSessionId}\u0000${input.runId}\u0000${payloadSha256}`,
        'utf8'
      )
    )
  }

  private async readStoredBatch(batchId: string): Promise<DurableTraceBatch> {
    const record = this.records.get(batchId)

    if (record === undefined) {
      throw new Error('unknown_trace_batch')
    }

    const encoded = await this.config.fs.readRange(this.config.segmentPath, record.offset, record.encodedBytes)

    if (encoded === null) {
      throw new Error('missing_trace_outbox_segment')
    }

    const decoded = decodeSegmentRecord(encoded, 0)

    if (decoded === null || decoded.header.batchId !== batchId) {
      throw new Error('invalid_trace_outbox_segment_index')
    }

    const body = await decryptTraceRecord(
      decoded.encrypted,
      this.config.dataKey,
      Buffer.from(`${decoded.header.owner.accountKey}/${decoded.header.batchId}`, 'utf8')
    )

    return { ...decoded.header, body }
  }

  private async ensureCapacity(incomingBytes: number, now: number): Promise<void> {
    if (!Number.isSafeInteger(incomingBytes) || incomingBytes < 0) {
      throw new RangeError('invalid_trace_outbox_capacity')
    }

    await this.expireBefore(now - this.config.retentionMs)

    while (true) {
      const disk = await this.readFreeSpace()
      const reserve = Math.max(ONE_GIB, Math.ceil(disk.total * 0.05))

      if (
        this.payloadBytes() + incomingBytes <= this.config.capacityBytes &&
        disk.available - incomingBytes >= reserve
      ) {
        return
      }

      const oldest = this.oldestUnsentOrQuarantined()

      if (oldest === undefined) {
        throw new Error('storage_unavailable')
      }

      await this.recordTerminal(oldest, { terminal: 'evicted' })
      this.evictedCapacity += 1
      await this.compactForCapacity()
    }
  }

  private async expireBefore(cutoff: number): Promise<void> {
    if (!Number.isSafeInteger(cutoff)) {
      throw new RangeError('invalid_trace_outbox_expiry')
    }

    for (const record of this.records.values()) {
      if ((record.state === 'pending' || record.state === 'quarantined') && record.batch.createdAt < cutoff) {
        await this.recordTerminal(record, { terminal: 'evicted' })
        this.expired += 1
      }
    }

    await this.compactIfIdle()
  }

  private oldestUnsentOrQuarantined(): StoredRecord | undefined {
    return [...this.records.values()]
      .filter(record => record.state === 'pending' || record.state === 'quarantined')
      .sort(
        (left, right) =>
          left.batch.createdAt - right.batch.createdAt ||
          left.batch.sequence - right.batch.sequence ||
          left.batch.batchId.localeCompare(right.batch.batchId)
      )[0]
  }

  private async reclaimFullyTerminalSegments(): Promise<boolean> {
    if (
      this.records.size === 0 ||
      [...this.records.values()].some(record => record.state !== 'receipt' && record.state !== 'terminal')
    ) {
      return false
    }

    await this.config.fs.unlink(this.config.segmentPath)
    await this.config.fs.syncDirectory(join(this.config.segmentPath, '..'))
    this.records.clear()
    this.segmentOffset = 0

    return true
  }

  private async compactSegmentIfNeeded(): Promise<boolean> {
    const live = [...this.records.values()]
      .filter(record => record.state === 'pending' || record.state === 'quarantined')
      .sort((left, right) => left.offset - right.offset)

    if (live.length === this.records.size || live.length === 0) {
      return false
    }

    const rewritten: Array<{ encoded: Buffer; record: StoredRecord }> = []

    for (const record of live) {
      const encoded = await this.config.fs.readRange(this.config.segmentPath, record.offset, record.encodedBytes)

      if (encoded === null || encoded.length !== record.encodedBytes || decodeSegmentRecord(encoded, 0) === null) {
        throw new Error('invalid_trace_outbox_segment_index')
      }

      rewritten.push({ encoded, record })
    }

    const temporaryPath = `${this.config.segmentPath}.compact-${randomUUID()}`
    await this.config.fs.writeFile(temporaryPath, Buffer.concat(rewritten.map(item => item.encoded)), {
      exclusive: true
    })
    await this.config.fs.syncFile(temporaryPath)
    await this.config.fs.replaceFile(temporaryPath, this.config.segmentPath)
    await this.config.fs.syncDirectory(dirname(this.config.segmentPath))

    let offset = 0

    for (const item of rewritten) {
      item.record.offset = offset
      offset += item.encoded.length
    }

    for (const [batchId, record] of this.records) {
      if (record.state === 'receipt' || record.state === 'terminal') {
        this.records.delete(batchId)
      }
    }

    this.segmentOffset = offset

    return true
  }

  private async compactJournal(): Promise<void> {
    const rewrite = this.journalTail.then(async () => {
      const recovered = await this.config.journal.recover()
      const retained = recovered.operations.filter(operation => {
        if (operation.op === 'owner' || operation.op === 'receipt') {
          return false
        }

        if (operation.op === 'terminal') {
          const record = this.records.get(operation.batchId)

          return record?.state === 'quarantined'
        }

        const record = this.records.get(operation.batchId)

        return record?.state === 'pending' || record?.state === 'quarantined'
      })

      if (this.accountKey !== null || this.legacyOwnerUnknown) {
        retained.unshift({ op: 'owner', accountKey: this.accountKey })
      }

      const receipts = [...this.receipts.values()]
        .sort((left, right) => left.receivedAt - right.receivedAt || left.batchId.localeCompare(right.batchId))
        .map(receipt => this.receiptOperation(receipt))
      retained.push(...receipts)

      await this.config.journal.replace(retained)
      this.ownerJournaled = this.accountKey !== null || this.legacyOwnerUnknown
    })

    this.journalTail = rewrite.catch(() => undefined)
    await rewrite
  }

  private async recordTerminal(
    record: StoredRecord,
    terminal: Omit<TraceJournalTerminalOperation, 'batchId' | 'op'>
  ): Promise<void> {
    await this.appendAndSyncJournal([{ batchId: record.batch.batchId, op: 'terminal', ...terminal }])
    record.state = terminal.terminal === 'quarantined' ? 'quarantined' : 'terminal'
    record.batch.lastErrorClass =
      terminal.terminal === 'quarantined' ? terminal.errorClass : record.batch.lastErrorClass

    if (terminal.terminal === 'evicted') {
      this.dedupe.delete(this.dedupeKey(record.batch))
    }
  }

  private payloadBytes(): number {
    return [...this.records.values()]
      .filter(record => record.state === 'pending' || record.state === 'quarantined')
      .reduce((total, record) => total + record.encodedBytes, 0)
  }

  private receiptBytes(): number {
    return [...this.receipts.values()].reduce((total, receipt) => total + this.receiptBytesFor(receipt), 0)
  }

  private receiptBytesFor(receipt: ReceiptTombstone): number {
    return traceJournalOperationBytes(this.receiptOperation(receipt))
  }

  private receiptOperation(receipt: ReceiptTombstone): TraceJournalReceiptOperation {
    return receipt.accountKey === null || receipt.dedupeKey === null
      ? {
          op: 'receipt',
          batchId: receipt.batchId,
          outcome: receipt.outcome,
          receivedAt: receipt.receivedAt
        }
      : {
          op: 'receipt',
          accountKey: receipt.accountKey,
          batchId: receipt.batchId,
          dedupeKey: receipt.dedupeKey,
          outcome: receipt.outcome,
          receivedAt: receipt.receivedAt
        }
  }

  private pruneReceipts(now: number): boolean {
    const ordered = [...this.receipts.values()].sort(
      (left, right) => left.receivedAt - right.receivedAt || left.batchId.localeCompare(right.batchId)
    )
    let pruned = false

    for (const receipt of ordered) {
      if (now - receipt.receivedAt > this.config.retentionMs) {
        pruned = this.deleteReceipt(receipt) || pruned
      }
    }

    let bytes = this.receiptBytes()

    for (const receipt of ordered) {
      if (this.receipts.size <= this.config.receiptCapacityEntries && bytes <= this.config.receiptCapacityBytes) {
        break
      }

      if (this.deleteReceipt(receipt)) {
        pruned = true
        bytes -= this.receiptBytesFor(receipt)
      }
    }

    return pruned
  }

  private deleteReceipt(receipt: ReceiptTombstone): boolean {
    if (!this.receipts.delete(receipt.batchId)) {
      return false
    }

    if (receipt.dedupeKey !== null && this.dedupe.get(receipt.dedupeKey) === receipt.batchId) {
      const record = this.records.get(receipt.batchId)
      if (record === undefined || record.state === 'receipt' || record.state === 'terminal') {
        this.dedupe.delete(receipt.dedupeKey)
      }
    }

    return true
  }

  private async readFreeSpace(): Promise<TraceFreeSpace> {
    const disk = await this.config.freeSpace()

    if (
      disk === null ||
      typeof disk !== 'object' ||
      !Number.isSafeInteger(disk.available) ||
      !Number.isSafeInteger(disk.total) ||
      disk.available < 0 ||
      disk.total < 0
    ) {
      throw new Error('invalid_trace_outbox_free_space')
    }

    return disk
  }

  private quarantineForKeyLoss(): void {
    this.segmentWritable = false

    for (const record of this.records.values()) {
      if (record.state === 'pending') {
        record.state = 'quarantined'
        record.batch.lastErrorClass = 'key_loss'
      }
    }
  }

  private async appendAndSyncJournal(operations: TraceJournalOperation[]): Promise<void> {
    const write = this.journalTail.then(async () => {
      await this.config.journal.append(operations)
      await this.config.journal.sync()
    })

    this.journalTail = write.catch(() => undefined)
    await write
  }

  private async withWriterLock<T>(operation: () => Promise<T>): Promise<T> {
    const prior = this.writerTail
    let release!: () => void
    this.writerTail = new Promise<void>(resolve => {
      release = resolve
    })
    await prior

    try {
      return await operation()
    } finally {
      release()
    }
  }
}
