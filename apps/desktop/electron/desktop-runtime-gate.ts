export type DesktopRuntimeState = 'failed' | 'not-ready' | 'preparing' | 'ready'

export class DesktopRuntimeGate {
  private generation = 0
  private inFlight: Promise<void> | null = null
  private runtimeState: DesktopRuntimeState = 'not-ready'

  get ready(): boolean {
    return this.runtimeState === 'ready'
  }

  get state(): DesktopRuntimeState {
    return this.runtimeState
  }

  rendererStatus<T extends { state: string }>(status: T): T & { runtime_ready: boolean } {
    return {
      ...status,
      runtime_ready: status.state === 'authenticated' && this.ready
    }
  }

  prepare(task: () => Promise<void>): Promise<void> {
    if (this.ready) {
      return Promise.resolve()
    }

    if (this.inFlight) {
      return this.inFlight
    }

    const generation = this.generation
    this.runtimeState = 'preparing'

    let started: Promise<void>

    try {
      started = task()
    } catch (error) {
      started = Promise.reject(error)
    }

    const operation = Promise.resolve(started)
      .then(() => {
        if (generation !== this.generation) {
          throw new Error('RUNTIME_PREPARATION_CANCELLED')
        }

        this.runtimeState = 'ready'
      })
      .catch(error => {
        if (generation === this.generation) {
          this.runtimeState = 'failed'
        }

        throw error
      })
      .finally(() => {
        if (this.inFlight === operation) {
          this.inFlight = null
        }
      })

    this.inFlight = operation

    return operation
  }

  invalidate(): void {
    this.generation += 1
    this.runtimeState = 'not-ready'
    this.inFlight = null
  }
}
