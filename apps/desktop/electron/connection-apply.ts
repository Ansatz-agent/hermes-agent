import { type ConnectionScope, requireAuthenticatedConnectionScope } from './auth-bridge'

export interface ForegroundConnectionGrant {
  connectionId: string
  scope: ConnectionScope
  token: string
}

interface ForegroundConnectionChangeOptions {
  current: ForegroundConnectionGrant | null
  nextConnectionId: string
  publish: (grant: ForegroundConnectionGrant) => Promise<void> | void
  requestFreshToken: (scope: ConnectionScope) => Promise<string>
  requireScope: (connectionId: string) => Promise<ConnectionScope>
  revokeForeground: (grant: ForegroundConnectionGrant) => Promise<void> | void
}

async function applyForegroundConnectionChange({
  current,
  nextConnectionId,
  publish,
  requestFreshToken,
  requireScope,
  revokeForeground
}: ForegroundConnectionChangeOptions): Promise<ForegroundConnectionGrant> {
  if (current) {
    await revokeForeground(current)
  }

  const connectionId = String(nextConnectionId || '').trim()
  const scope = requireAuthenticatedConnectionScope(await requireScope(connectionId))

  if (!connectionId || scope.connection_id !== connectionId) {
    throw new Error('AUTH_REQUIRED')
  }

  const token = await requestFreshToken(scope)

  if (!token) {
    throw new Error('AUTH_REQUIRED')
  }

  const grant = { connectionId, scope: { ...scope }, token }
  await publish(grant)

  return grant
}

async function applyConnectionChange({
  cancelAndWait,
  isPrimary,
  rehomePrimary = null,
  scope,
  sendApplied,
  stopPool,
  teardownPrimary,
  teardownSsh
}) {
  await cancelAndWait(scope)
  await teardownSsh(scope)

  if (!isPrimary) {
    stopPool(scope)

    return
  }

  if (rehomePrimary) {
    await rehomePrimary()

    return
  }

  await teardownPrimary()
  sendApplied()
}

function commitConnectionFailure(current, starting, commit) {
  if (current !== starting) {
    return false
  }

  commit()

  return true
}

async function resolveTerminalConnection(getTarget, ensureBackend) {
  let target = getTarget()

  if (target !== 'pending') {
    return target
  }

  await ensureBackend()
  target = getTarget()

  if (target === 'pending') {
    throw new Error('Remote connection is not ready yet. Try again in a moment.')
  }

  return target
}

export { applyConnectionChange, applyForegroundConnectionChange, commitConnectionFailure, resolveTerminalConnection }
