import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { join } from 'node:path'

import { decryptTraceRecord, encryptTraceRecord, type TraceKeyProtector } from './trace-outbox-crypto'
import {
  nodeTraceFileSystem,
  type TraceFileSystem,
  TraceJournal,
  type TraceJournalOperation,
  type TraceJournalPendingOperation,
  type TraceJournalReceiptOperation
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
  validateTraceOwner
} from './trace-outbox-types'

const DEFAULT_GROUP_COMMIT_BYTES = 8 * 1024 * 1024
const DEFAULT_GROUP_COMMIT_MS = 50
const KEY_FILE_NAME = 'key.json'
const SEGMENT_FILE_NAME = 'active.segment'
const JOURNAL_FILE_NAME = 'index.journal'

interface PersistedKey {
  version: 1
  wrappedKey: string
}

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
  state: 'pending' | 'quarantined' | 'receipt'
}

export interface TraceOutboxStoreOptions {
  fs?: TraceFileSystem
  groupCommitBytes?: number
  groupCommitMs?: number
  keyProtector: TraceKeyProtector
  monotonicNow?: () => number
  now?: () => number
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

function keyJson(wrappedKey: Buffer): Buffer {
  const encoded: PersistedKey = { version: 1, wrappedKey: wrappedKey.toString('base64') }

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
    (parsed as Record<string, unknown>).version !== 1 ||
    typeof (parsed as Record<string, unknown>).wrappedKey !== 'string'
  ) {
    throw new Error('invalid_trace_outbox_key')
  }

  const wrappedKey = (parsed as PersistedKey).wrappedKey

  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(wrappedKey) || wrappedKey.length % 4 !== 0) {
    throw new Error('invalid_trace_outbox_key')
  }

  return { version: 1, wrappedKey }
}

export class TraceOutboxStore {
  static async open(options: TraceOutboxStoreOptions): Promise<TraceOutboxStore> {
    const fs = options.fs ?? nodeTraceFileSystem
    const groupCommitBytes = options.groupCommitBytes ?? DEFAULT_GROUP_COMMIT_BYTES
    const groupCommitMs = options.groupCommitMs ?? DEFAULT_GROUP_COMMIT_MS

    if (!Number.isSafeInteger(groupCommitBytes) || groupCommitBytes <= 0) {
      throw new RangeError('invalid_group_commit_bytes')
    }

    if (!Number.isSafeInteger(groupCommitMs) || groupCommitMs < 0 || groupCommitMs > DEFAULT_GROUP_COMMIT_MS) {
      throw new RangeError('invalid_group_commit_ms')
    }

    await fs.mkdir(options.root)
    const segmentDirectory = join(options.root, 'segments')
    await fs.mkdir(segmentDirectory)
    const keyPath = join(options.root, KEY_FILE_NAME)
    const dataKey = await TraceOutboxStore.loadOrCreateKey(fs, keyPath, options.root, options.keyProtector)
    const journal = await TraceJournal.open({ fs, path: join(options.root, JOURNAL_FILE_NAME) })

    const store = new TraceOutboxStore({
      dataKey,
      fs,
      groupCommitBytes,
      groupCommitMs,
      journal,
      monotonicNow: options.monotonicNow ?? performance.now.bind(performance),
      now: options.now ?? Date.now,
      segmentPath: join(segmentDirectory, SEGMENT_FILE_NAME)
    })

    await store.recover()

    return store
  }

  private static async loadOrCreateKey(
    fs: TraceFileSystem,
    keyPath: string,
    root: string,
    keyProtector: TraceKeyProtector
  ): Promise<Buffer> {
    if (!keyProtector.available()) {
      throw new Error('secure_key_storage_unavailable')
    }

    const existing = await fs.readFile(keyPath)

    if (existing !== null) {
      return keyProtector.unwrap(Buffer.from(parseKey(existing).wrappedKey, 'base64'))
    }

    const dataKey = randomBytes(32)
    const temporaryPath = `${keyPath}.tmp-${randomUUID()}`
    await fs.writeFile(temporaryPath, keyJson(keyProtector.wrap(dataKey)), { exclusive: true })
    await fs.syncFile(temporaryPath)
    await fs.rename(temporaryPath, keyPath)
    await fs.syncDirectory(root)

    return dataKey
  }

  private readonly pending: PendingCommit[] = []
  private readonly receipts = new Map<string, DurableReceipt>()
  private readonly records = new Map<string, StoredRecord>()
  private flushPromise: Promise<void> | null = null
  private flushTimer: ReturnType<typeof setTimeout> | null = null
  private journalTail: Promise<void> = Promise.resolve()
  private nextSequence = 0
  private pendingInputBytes = 0
  private pendingDeadline: number | null = null
  private segmentOffset = 0
  private segmentWritable = true
  private quarantinedSegments = 0
  private recoveredCorruptTail = 0

  private constructor(
    private readonly config: {
      dataKey: Buffer
      fs: TraceFileSystem
      groupCommitBytes: number
      groupCommitMs: number
      journal: TraceJournal
      monotonicNow: () => number
      now: () => number
      segmentPath: string
    }
  ) {}

  beginEnqueue(input: TraceEnvelopeInput): PendingLocalCommit {
    validateTraceOwner(input.owner)

    if (!Buffer.isBuffer(input.body)) {
      throw new TypeError('invalid_trace_payload')
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

  async diagnostics(): Promise<TraceOutboxStoreDiagnostics> {
    let payloadBytes = 0
    let pending = 0

    for (const record of this.records.values()) {
      if (record.state !== 'pending') {
        continue
      }

      payloadBytes += record.encodedBytes
      pending += 1
    }

    return {
      accepted: 0,
      deduplicated: 0,
      duplicate: 0,
      evictedCapacity: 0,
      expired: 0,
      payloadBytes,
      pending,
      pendingBytes: payloadBytes,
      quarantined: this.quarantinedSegments,
      recoveredCorruptTail: this.recoveredCorruptTail
    }
  }

  async lookupReceipt(batchId: string): Promise<DurableReceipt | undefined> {
    const receipt = this.receipts.get(batchId)

    return receipt === undefined ? undefined : { ...receipt }
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
      const segment = await this.config.fs.readFile(this.config.segmentPath)

      if (segment === null) {
        throw new Error('missing_trace_outbox_segment')
      }

      const decoded = decodeSegmentRecord(segment, head.offset)

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

  private async recover(): Promise<void> {
    const scanned = await this.scanActiveSegment()
    const recovered = await this.config.journal.recover()
    const pendingOperations = new Set<string>()

    for (const operation of recovered.operations) {
      if (operation.op === 'receipt') {
        this.receipts.set(operation.batchId, { ...operation })
      }

      if (operation.op === 'pending') {
        pendingOperations.add(operation.batchId)
        this.nextSequence = Math.max(this.nextSequence, operation.sequence + 1)
      }
    }

    for (const record of scanned.records) {
      this.nextSequence = Math.max(this.nextSequence, record.header.sequence + 1)
      const receipt = this.receipts.get(record.header.batchId)

      this.records.set(record.header.batchId, {
        batch: record.header,
        encodedBytes: record.encodedBytes,
        offset: record.offset,
        state: receipt === undefined ? 'pending' : 'receipt'
      })
    }

    this.segmentOffset = scanned.nextOffset
    this.segmentWritable = !scanned.quarantined
    this.quarantinedSegments = scanned.quarantined ? 1 : 0
    this.recoveredCorruptTail = Number(scanned.recoveredTornTail || recovered.recoveredTornTail)

    if (scanned.recoveredTornTail) {
      await this.config.fs.writeFile(this.config.segmentPath, scanned.trustedPrefix)
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
      await this.appendAndSyncJournal(reconstructed)
    }
  }

  private async scanActiveSegment(): Promise<{
    nextOffset: number
    quarantined: boolean
    records: Array<{ encodedBytes: number; header: TraceSegmentHeader; offset: number }>
    recoveredTornTail: boolean
    trustedPrefix: Buffer
  }> {
    const segment = await this.config.fs.readFile(this.config.segmentPath)

    if (segment === null || segment.length === 0) {
      return {
        nextOffset: 0,
        quarantined: false,
        records: [],
        recoveredTornTail: false,
        trustedPrefix: Buffer.alloc(0)
      }
    }

    const records: Array<{ encodedBytes: number; header: TraceSegmentHeader; offset: number }> = []
    let offset = 0

    while (offset < segment.length) {
      try {
        const decoded = decodeSegmentRecord(segment, offset)

        if (decoded === null) {
          return {
            nextOffset: offset,
            quarantined: false,
            records,
            recoveredTornTail: true,
            trustedPrefix: Buffer.from(segment.subarray(0, offset))
          }
        }

        records.push({ encodedBytes: decoded.nextOffset - offset, header: decoded.header, offset })
        offset = decoded.nextOffset
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
      trustedPrefix: Buffer.from(segment)
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
    if (this.pending.length === 0 || this.flushPromise !== null) {
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

    const currentFlush = this.flushGroup(group)
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

  private async flushGroup(group: PendingCommit[]): Promise<void> {
    try {
      if (!this.segmentWritable) {
        throw new Error('trace_outbox_segment_quarantined')
      }

      const records = await Promise.all(group.map(item => this.prepareRecord(item)))
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

      const receiptOperations: TraceJournalReceiptOperation[] = group
        .filter(item => item.cancellation !== null)
        .map(item => ({ op: 'receipt', ...item.cancellation! }))

      await this.appendAndSyncJournal([...pendingOperations, ...receiptOperations])

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
          this.receipts.set(receipt.batchId, { ...receipt })
        }

        item.resolve(record.batch)
      }
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
    if (receipt.batchId !== item.batchId) {
      return Promise.reject(new TypeError('receipt_batch_mismatch'))
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

      this.pendingInputBytes -= item.input?.body.length ?? 0
      item.input = null
      item.state = 'cancelled'

      try {
        await this.persistReceipt(receipt)
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
        return
      }
    }

    if (item.state === 'committed' || item.state === 'flushing') {
      await this.persistReceipt(receipt)
    }
  }

  private async persistReceipt(receipt: DurableReceipt): Promise<void> {
    await this.appendAndSyncJournal([{ op: 'receipt', ...receipt }])
    this.receipts.set(receipt.batchId, { ...receipt })
    const record = this.records.get(receipt.batchId)

    if (record !== undefined) {
      record.state = 'receipt'
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
}
