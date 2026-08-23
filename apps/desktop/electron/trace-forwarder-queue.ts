export type TraceQueueBatch = {
  body: Buffer
  contentType: 'application/x-protobuf'
  entrypoint: 'desktop' | 'voice' | 'cli' | 'dashboard'
  epoch: number
  runId: string
  sessionId: string
  telemetrySchemaVersion: string
}

export type TraceQueueSendResult = 'sent' | 'retry' | 'drop'

export type TraceForwarderQueueSummary = {
  discarded: number
  dropped: number
  expired: number
  queued: number
  queuedBytes: number
  retried: number
  sent: number
}

type QueueItem = TraceQueueBatch & {
  attempts: number
  enqueuedAt: number
  nextAttemptAt: number
}

type TraceForwarderQueueOptions = {
  clock?: () => number
  jitter?: (delayMs: number) => number
  maxAgeMs?: number
  maxBytes?: number
  maxItems?: number
}

const DEFAULT_MAX_ITEMS = 128
const DEFAULT_MAX_BYTES = 32 * 1024 * 1024
const DEFAULT_MAX_AGE_MS = 15 * 60 * 1_000
const RETRY_DELAYS_MS = Object.freeze([1_000, 2_000, 4_000, 8_000, 16_000, 30_000])

export class TraceForwarderQueue {
  private readonly clock: () => number
  private readonly jitter: (delayMs: number) => number
  private readonly maxAgeMs: number
  private readonly maxBytes: number
  private readonly maxItems: number
  private readonly items: QueueItem[] = []
  private activeEpoch: number | null = null
  private pumping: Promise<void> | null = null
  private queuedBytes = 0
  private counters = {
    discarded: 0,
    dropped: 0,
    expired: 0,
    retried: 0,
    sent: 0
  }

  constructor(options: TraceForwarderQueueOptions = {}) {
    this.clock = options.clock ?? Date.now
    this.jitter = options.jitter ?? defaultJitter
    this.maxAgeMs = options.maxAgeMs ?? DEFAULT_MAX_AGE_MS
    this.maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES
    this.maxItems = options.maxItems ?? DEFAULT_MAX_ITEMS
  }

  activateEpoch(epoch: number): void {
    if (!Number.isSafeInteger(epoch) || epoch < 0) {
      throw new TypeError('invalid_epoch')
    }

    if (this.activeEpoch === epoch) {
      return
    }

    this.counters.discarded += this.items.length
    this.items.length = 0
    this.queuedBytes = 0
    this.activeEpoch = epoch
  }

  enqueue(batch: TraceQueueBatch): { accepted: boolean; dropped: number } {
    if (batch.epoch !== this.activeEpoch || batch.body.length > this.maxBytes) {
      this.counters.dropped += 1

      return { accepted: false, dropped: 1 }
    }

    const before = this.counters.dropped
    const body = Buffer.from(batch.body)
    const now = this.clock()

    this.items.push({ ...batch, body, attempts: 0, enqueuedAt: now, nextAttemptAt: now })
    this.queuedBytes += body.length

    while (this.items.length > this.maxItems || this.queuedBytes > this.maxBytes) {
      this.dropOldest('dropped')
    }

    return { accepted: true, dropped: this.counters.dropped - before }
  }

  pump(
    send: (batch: TraceQueueBatch) => Promise<TraceQueueSendResult>,
    options: { ignoreBackoff?: boolean } = {}
  ): Promise<void> {
    if (this.pumping) {
      return this.pumping
    }

    this.pumping = this.pumpExclusive(send, options).finally(() => {
      this.pumping = null
    })

    return this.pumping
  }

  nextRetryAt(): number | null {
    this.expireOldItems()

    return this.items[0]?.nextAttemptAt ?? null
  }

  clear(): void {
    this.counters.discarded += this.items.length
    this.items.length = 0
    this.queuedBytes = 0
    this.activeEpoch = null
  }

  summary(): TraceForwarderQueueSummary {
    return {
      ...this.counters,
      queued: this.items.length,
      queuedBytes: this.queuedBytes
    }
  }

  inspectForTest(): readonly TraceQueueBatch[] {
    return this.items
  }

  private async pumpExclusive(
    send: (batch: TraceQueueBatch) => Promise<TraceQueueSendResult>,
    { ignoreBackoff = false }: { ignoreBackoff?: boolean }
  ): Promise<void> {
    this.expireOldItems()

    while (this.items.length > 0) {
      const current = this.items[0]

      if (current.epoch !== this.activeEpoch) {
        this.dropOldest('discarded')

        continue
      }

      const now = this.clock()

      if (!ignoreBackoff && now < current.nextAttemptAt) {
        return
      }

      const result = await send(current)

      if (this.items[0] !== current) {
        return
      }

      if (result === 'sent') {
        this.counters.sent += 1
        this.shift()

        continue
      }

      if (result === 'drop') {
        this.dropOldest('dropped')

        continue
      }

      this.counters.retried += 1
      const delay = RETRY_DELAYS_MS[Math.min(current.attempts, RETRY_DELAYS_MS.length - 1)]

      current.attempts += 1
      current.nextAttemptAt = now + Math.max(0, Math.round(this.jitter(delay)))

      return
    }
  }

  private expireOldItems(): void {
    const now = this.clock()

    while (this.items.length > 0 && now - this.items[0].enqueuedAt >= this.maxAgeMs) {
      this.dropOldest('expired')
    }
  }

  private dropOldest(counter: 'discarded' | 'dropped' | 'expired'): void {
    if (this.items.length === 0) {
      return
    }

    this.counters[counter] += 1
    this.shift()
  }

  private shift(): void {
    const removed = this.items.shift()

    if (removed) {
      this.queuedBytes -= removed.body.length
    }
  }
}

function defaultJitter(delayMs: number): number {
  return delayMs * (0.8 + Math.random() * 0.4)
}
