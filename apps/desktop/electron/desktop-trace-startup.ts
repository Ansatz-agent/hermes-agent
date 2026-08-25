export async function prepareLocalTraceCapture<T>({
  startListener,
  onDiagnostic,
  scheduleRetry
}: {
  startListener: () => Promise<T>
  onDiagnostic: (message: string) => void
  scheduleRetry: () => void
}): Promise<T | null> {
  try {
    return await startListener()
  } catch {
    onDiagnostic('trace capture unavailable')
    scheduleRetry()

    return null
  }
}

type LocalTraceCaptureOptions<Scope, Capture> = {
  isScopeCurrent: (scope: Scope) => boolean
  onDiagnostic: (message: string) => void
  retryMs: number
  sameScope: (left: Scope, right: Scope) => boolean
  setupTimeoutMs: number
  startCapture: (scope: Scope) => Promise<Capture>
  stopCapture: (capture: Capture, flushMs: number) => Promise<void>
}

type CaptureAttempt<Scope, Capture> = {
  generation: number
  promise: Promise<Capture>
  reject: (error: Error) => void
  resolve: (capture: Capture) => void
  scope: Scope
  timeout: ReturnType<typeof setTimeout> | null
}

type ActiveCapture<Scope, Capture> = {
  capture: Capture
  scope: Scope
}

export class LocalTraceCaptureController<Scope, Capture> {
  private activeCapture: ActiveCapture<Scope, Capture> | null = null
  private activeAttempt: CaptureAttempt<Scope, Capture> | null = null
  private generation = 0
  private retryScope: Scope | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null

  constructor(private readonly options: LocalTraceCaptureOptions<Scope, Capture>) {}

  context(scope: Scope): Capture | null {
    return this.activeCapture && this.options.sameScope(this.activeCapture.scope, scope)
      ? this.activeCapture.capture
      : null
  }

  current(): Capture | null {
    return this.activeCapture?.capture ?? null
  }

  async prepare(scope: Scope): Promise<Capture | null> {
    return prepareLocalTraceCapture({
      startListener: () => this.start(scope),
      onDiagnostic: this.options.onDiagnostic,
      scheduleRetry: () => this.scheduleRetry(scope)
    })
  }

  async stop(flushMs = 0): Promise<void> {
    this.cancelRetry()

    if (this.activeAttempt) {
      this.detachAttempt(this.activeAttempt, new Error('trace_capture_stopped'))
    }

    const active = this.activeCapture

    this.activeCapture = null

    if (active) {
      await this.options.stopCapture(active.capture, flushMs)
    }
  }

  private start(scope: Scope): Promise<Capture> {
    if (!this.options.isScopeCurrent(scope)) {
      return Promise.reject(new Error('trace_capture_scope_stale'))
    }

    const active = this.activeCapture

    if (active && this.options.sameScope(active.scope, scope)) {
      return Promise.resolve(active.capture)
    }

    if (active) {
      this.activeCapture = null
      this.stopStaleCapture(active.capture)
    }

    if (this.activeAttempt) {
      if (this.options.sameScope(this.activeAttempt.scope, scope)) {
        return this.activeAttempt.promise
      }

      this.detachAttempt(this.activeAttempt, new Error('trace_capture_scope_superseded'))
    }

    return this.createAttempt(scope)
  }

  private createAttempt(scope: Scope): Promise<Capture> {
    const generation = ++this.generation
    let resolve: (capture: Capture) => void
    let reject: (error: Error) => void

    const promise = new Promise<Capture>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise
      reject = rejectPromise
    })

    const attempt: CaptureAttempt<Scope, Capture> = {
      generation,
      promise,
      reject: reject!,
      resolve: resolve!,
      scope,
      timeout: null
    }

    this.activeAttempt = attempt
    attempt.timeout = setTimeout(() => {
      this.detachAttempt(attempt, new Error('trace_capture_setup_timeout'))
    }, this.options.setupTimeoutMs)
    attempt.timeout.unref?.()

    void Promise.resolve()
      .then(() => this.options.startCapture(scope))
      .then(capture => this.completeAttempt(attempt, capture))
      .catch(error => this.failAttempt(attempt, error))

    return promise
  }

  private completeAttempt(attempt: CaptureAttempt<Scope, Capture>, capture: Capture): void {
    if (
      this.activeAttempt !== attempt ||
      this.generation !== attempt.generation ||
      !this.options.isScopeCurrent(attempt.scope)
    ) {
      this.stopStaleCapture(capture)

      return
    }

    this.clearAttemptTimeout(attempt)
    this.activeAttempt = null
    this.activeCapture = { capture, scope: attempt.scope }
    attempt.resolve(capture)
  }

  private failAttempt(attempt: CaptureAttempt<Scope, Capture>, error: unknown): void {
    if (this.activeAttempt === attempt && this.generation === attempt.generation) {
      this.detachAttempt(attempt, error instanceof Error ? error : new Error('trace_capture_unavailable'))
    }
  }

  private detachAttempt(attempt: CaptureAttempt<Scope, Capture>, error: Error): void {
    if (this.activeAttempt !== attempt) {
      return
    }

    this.clearAttemptTimeout(attempt)
    this.activeAttempt = null
    this.generation += 1
    attempt.reject(error)
  }

  private scheduleRetry(scope: Scope): void {
    if (!this.options.isScopeCurrent(scope)) {
      return
    }

    if (this.retryTimer && this.retryScope && this.options.sameScope(this.retryScope, scope)) {
      return
    }

    this.cancelRetry()
    this.retryScope = scope
    this.retryTimer = setTimeout(() => {
      const retryScope = this.retryScope

      this.retryScope = null
      this.retryTimer = null

      if (!retryScope || !this.options.isScopeCurrent(retryScope)) {
        return
      }

      void this.prepare(retryScope).catch(() => {})
    }, this.options.retryMs)
    this.retryTimer.unref?.()
  }

  private cancelRetry(): void {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }

    this.retryScope = null
  }

  private clearAttemptTimeout(attempt: CaptureAttempt<Scope, Capture>): void {
    if (attempt.timeout) {
      clearTimeout(attempt.timeout)
      attempt.timeout = null
    }
  }

  private stopStaleCapture(capture: Capture): void {
    void this.options.stopCapture(capture, 0).catch(() => {})
  }
}
