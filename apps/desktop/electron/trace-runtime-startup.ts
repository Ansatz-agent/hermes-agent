type TraceStartupState = 'idle' | 'starting' | 'degraded' | 'ready'
type TraceTimer = number | ReturnType<typeof setTimeout>

type TraceRuntimeStartupRecoveryOptions = {
  clearTimer?: (timer: TraceTimer) => void
  isRecoverable?: (error: unknown) => boolean
  setTimer?: (callback: () => void, delay: number) => TraceTimer
}

type ResolveLocalBackendWithTraceOptions<TBackend> = {
  key: string
  recovery: TraceRuntimeStartupRecovery
  resolveBackend: () => TBackend | Promise<TBackend>
  startEncryptedTrace: () => Promise<unknown>
}

const INITIAL_RETRY_MS = 1_000
const MAX_RETRY_MS = 30_000

export class TraceRuntimeStartupRecovery {
  private readonly clearTimer: (timer: TraceTimer) => void
  private readonly isRecoverable: (error: unknown) => boolean
  private readonly setTimer: (callback: () => void, delay: number) => TraceTimer
  private attempt = 0
  private flight: Promise<void> | null = null
  private generation = 0
  private key: string | null = null
  private retryTimer: TraceTimer | null = null
  private startEncryptedTrace: (() => Promise<unknown>) | null = null
  private status: TraceStartupState = 'idle'

  constructor(options: TraceRuntimeStartupRecoveryOptions = {}) {
    this.clearTimer = options.clearTimer ?? clearTimeout
    this.isRecoverable = options.isRecoverable ?? (() => true)
    this.setTimer = options.setTimer ?? setTimeout
  }

  async prepare(key: string, startEncryptedTrace: () => Promise<unknown>): Promise<void> {
    if (!key) {
      throw new TypeError('invalid_trace_runtime_key')
    }

    if (this.key !== key) {
      this.reset()
      this.key = key
    }

    this.startEncryptedTrace = startEncryptedTrace
    await this.run(this.generation)
  }

  state(): TraceStartupState {
    return this.status
  }

  async stop(): Promise<void> {
    this.reset()
  }

  private reset(): void {
    this.generation += 1
    this.attempt = 0
    this.flight = null
    this.key = null
    this.startEncryptedTrace = null
    this.status = 'idle'

    if (this.retryTimer !== null) {
      this.clearTimer(this.retryTimer)
      this.retryTimer = null
    }
  }

  private async run(generation: number): Promise<void> {
    if (generation !== this.generation || this.startEncryptedTrace === null) {
      return
    }

    if (this.flight !== null) {
      await this.flight

      return
    }

    this.status = 'starting'
    const start = this.startEncryptedTrace

    const current = start()
      .then(() => {
        if (generation !== this.generation) {
          return
        }

        this.attempt = 0
        this.status = 'ready'
      })
      .catch(error => {
        if (generation !== this.generation) {
          return
        }

        if (!this.isRecoverable(error)) {
          this.status = 'idle'
          throw error
        }

        this.status = 'degraded'
        this.scheduleRetry(generation)
      })

    this.flight = current

    try {
      await current
    } finally {
      if (this.flight === current) {
        this.flight = null
      }
    }
  }

  private scheduleRetry(generation: number): void {
    if (this.retryTimer !== null || generation !== this.generation) {
      return
    }

    const delay = Math.min(MAX_RETRY_MS, INITIAL_RETRY_MS * 2 ** Math.min(this.attempt, 5))
    this.attempt += 1
    this.retryTimer = this.setTimer(() => {
      this.retryTimer = null
      void this.run(generation).catch(() => {})
    }, delay)
  }
}

export async function resolveLocalBackendWithTrace<TBackend>(
  options: ResolveLocalBackendWithTraceOptions<TBackend>
): Promise<TBackend> {
  await options.recovery.prepare(options.key, options.startEncryptedTrace)

  return await options.resolveBackend()
}
