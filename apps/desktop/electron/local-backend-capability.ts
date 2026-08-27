import type fs from 'node:fs'

import type { ConnectionScope } from './auth-bridge'
import { DESKTOP_SCOPE_PROTOCOL_VERSION } from './auth-scope-token'
import { BackendControlChannel, type ChildProcessLike } from './backend-control-channel'
import {
  LocalBackendCapabilityUnavailableError,
  LocalCapabilityManager,
  type LocalCapabilitySnapshot
} from './local-capability-manager'

export class LocalRuntimeProtocolError extends Error {
  readonly code = 'local_runtime_protocol_mismatch'

  constructor() {
    super('Local runtime does not support the required desktop scope protocol')
    this.name = 'LocalRuntimeProtocolError'
  }
}

export type PrepareLocalBackendCapabilityOptions = {
  backendGeneration: number
  child: ChildProcessLike
  isCurrent?: () => boolean
  key: string
  manager: LocalCapabilityManager
  onControlClosed?: (child: ChildProcessLike, reason: Error) => void
  onLog: (line: string) => void
  readyFile?: fs.PathOrFileDescriptor
  scope: ConnectionScope
  timeoutMs?: number
}

export type PreparedLocalBackendCapability = {
  baseUrl: string
  control: BackendControlChannel
  snapshot: LocalCapabilitySnapshot
}

type LifecycleBinding = {
  child: ChildProcessLike
  control: BackendControlChannel
  generation: number
  key: string
  onTerminated: () => void
}

type LocalCapabilityDescriptor = {
  authMode?: string
  localCapabilityKey?: string
  token?: string | null
}

function asError(value: unknown): Error {
  return value instanceof Error ? value : new Error('Local backend capability preparation failed')
}

function endControlInput(child: ChildProcessLike): void {
  if (child.stdin.destroyed || child.stdin.writable === false) {
    return
  }

  try {
    child.stdin.end()
  } catch {
    // Process teardown remains authoritative when the pipe has already gone.
  }
}

export async function prepareLocalBackendCapability(
  options: PrepareLocalBackendCapabilityOptions
): Promise<PreparedLocalBackendCapability> {
  const control = new BackendControlChannel(options.child, {
    onClose: reason => {
      endControlInput(options.child)
      options.onControlClosed?.(options.child, reason)
    },
    onLog: options.onLog
  })

  let activationStarted = false

  try {
    const ready = await control.waitForReady({
      readyFile: options.readyFile,
      timeoutMs: options.timeoutMs
    })

    if (ready.desktopScopeProtocol !== DESKTOP_SCOPE_PROTOCOL_VERSION) {
      throw new LocalRuntimeProtocolError()
    }

    if (options.isCurrent && !options.isCurrent()) {
      throw new LocalBackendCapabilityUnavailableError()
    }

    const baseUrl = `http://127.0.0.1:${ready.port}`
    activationStarted = true

    const snapshot = await options.manager.activate({
      key: options.key,
      baseUrl,
      scope: options.scope,
      backendGeneration: options.backendGeneration,
      control
    })

    return { baseUrl, control, snapshot }
  } catch (error) {
    if (activationStarted) {
      options.manager.revokeByControl(control)
    }

    control.close(asError(error))
    endControlInput(options.child)
    throw error
  }
}

export class LocalBackendCapabilityLifecycle {
  private readonly bindings = new Map<string, LifecycleBinding>()
  private readonly latestPreparations = new Map<string, number>()
  private readonly manager: LocalCapabilityManager
  private readonly onControlClosed: (child: ChildProcessLike, reason: Error) => void
  private backendGeneration = 0

  constructor(
    manager = new LocalCapabilityManager(),
    options: {
      onControlClosed?: (child: ChildProcessLike, reason: Error) => void
    } = {}
  ) {
    this.manager = manager
    this.onControlClosed = options.onControlClosed ?? (() => undefined)
  }

  async prepare(
    options: Omit<
      PrepareLocalBackendCapabilityOptions,
      'backendGeneration' | 'isCurrent' | 'manager' | 'onControlClosed'
    >
  ): Promise<PreparedLocalBackendCapability> {
    this.backendGeneration += 1
    const generation = this.backendGeneration
    this.latestPreparations.set(options.key, generation)

    const prepared = await prepareLocalBackendCapability({
      ...options,
      backendGeneration: generation,
      isCurrent: () => this.latestPreparations.get(options.key) === generation,
      manager: this.manager,
      onControlClosed: this.onControlClosed
    })

    if (this.latestPreparations.get(options.key) !== generation) {
      const error = new LocalBackendCapabilityUnavailableError()

      try {
        if (this.manager.snapshot(options.key).backendGeneration === generation) {
          this.manager.revoke(options.key)
        }
      } catch {
        // A newer generation may already own the manager state.
      }

      prepared.control.close(error)
      throw error
    }

    const previous = this.bindings.get(options.key)

    if (previous) {
      this.detach(previous)
      endControlInput(previous.child)
    }

    const binding: LifecycleBinding = {
      child: options.child,
      control: prepared.control,
      generation,
      key: options.key,
      onTerminated: () => this.release(binding)
    }

    this.bindings.set(options.key, binding)
    options.child.on('error', binding.onTerminated)
    options.child.on('exit', binding.onTerminated)

    return prepared
  }

  snapshot(key: string): LocalCapabilitySnapshot {
    return this.manager.snapshot(key)
  }

  snapshotDescriptor<T extends LocalCapabilityDescriptor>(descriptor: T): T {
    if (descriptor.authMode !== 'scope') {
      return descriptor
    }

    const snapshot = this.manager.snapshot(descriptor.localCapabilityKey ?? '')
    descriptor.token = snapshot.bearer

    return descriptor
  }

  revoke(key: string): void {
    this.latestPreparations.delete(key)
    const binding = this.bindings.get(key)

    if (!binding) {
      this.manager.revoke(key)

      return
    }

    this.release(binding)
    this.manager.revoke(key)
  }

  revokeByChild(child: ChildProcessLike | null | undefined): boolean {
    if (!child) {
      return false
    }

    for (const binding of this.bindings.values()) {
      if (binding.child === child) {
        this.release(binding)

        return true
      }
    }

    endControlInput(child)

    return false
  }

  private release(binding: LifecycleBinding): void {
    if (this.bindings.get(binding.key) !== binding) {
      return
    }

    this.bindings.delete(binding.key)

    if (this.latestPreparations.get(binding.key) === binding.generation) {
      this.latestPreparations.delete(binding.key)
    }

    this.manager.revokeByControl(binding.control)
    this.detach(binding)
    endControlInput(binding.child)
  }

  private detach(binding: LifecycleBinding): void {
    binding.child.off('error', binding.onTerminated)
    binding.child.off('exit', binding.onTerminated)
  }
}
