import type { BridgeStatus } from './auth-bridge'

export type AuthenticatedRuntimePreparationResult = 'ready' | 'current-failure' | 'stale'

type AuthenticatedRuntimePreparationOptions = {
  observedStatus: BridgeStatus
  prepare: () => Promise<void>
  currentStatus: () => BridgeStatus | null
  isAuthenticated: () => boolean
  onCurrentReady: (status: BridgeStatus) => Promise<void> | void
  onCurrentFailure: (error: unknown, status: BridgeStatus) => void
}

function exactCurrentStatus(options: AuthenticatedRuntimePreparationOptions): BridgeStatus | null {
  if (options.observedStatus.state !== 'authenticated' || !options.isAuthenticated()) {
    return null
  }

  const current = options.currentStatus()

  if (
    current?.state !== 'authenticated' ||
    current.runtime_instance_id !== options.observedStatus.runtime_instance_id ||
    current.epoch !== options.observedStatus.epoch
  ) {
    return null
  }

  return current
}

export async function runAuthenticatedRuntimePreparation(
  options: AuthenticatedRuntimePreparationOptions
): Promise<AuthenticatedRuntimePreparationResult> {
  if (!exactCurrentStatus(options)) {
    return 'stale'
  }

  try {
    await options.prepare()
    const current = exactCurrentStatus(options)

    if (!current) {
      return 'stale'
    }

    await options.onCurrentReady(current)

    return 'ready'
  } catch (error) {
    const current = exactCurrentStatus(options)

    if (!current) {
      return 'stale'
    }

    options.onCurrentFailure(error, current)

    return 'current-failure'
  }
}
