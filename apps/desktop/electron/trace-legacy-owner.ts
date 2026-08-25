import { createHash } from 'node:crypto'
import { join } from 'node:path'

import type { ConnectionScope } from './auth-bridge'
import type { TraceOwner } from './trace-outbox-types'
import { legacyTraceOwner } from './trace-recovery-controller'

const LEGACY_PRINCIPAL_KEY = /^legacy:([0-9a-f]{64})$/
const ACCOUNT_PRINCIPAL_KEY = /^account:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

type LegacyTraceMigrationOptions = {
  currentAccountKey: string
  previousAccountKey: string
  rename: (source: string, destination: string) => Promise<void>
  root: string
}

export function legacyTraceOwnerForPrincipal(principalKey: string, installationId: string): TraceOwner {
  const match = LEGACY_PRINCIPAL_KEY.exec(principalKey)

  if (match === null) {
    throw new TypeError('invalid_legacy_principal_key')
  }

  return legacyTraceOwner(match[1], installationId)
}

export function localOnlyTraceOwnerForPrincipal(principalKey: string, installationId: string): TraceOwner {
  const legacy = LEGACY_PRINCIPAL_KEY.exec(principalKey)

  if (legacy !== null) {
    return legacyTraceOwner(legacy[1], installationId)
  }

  if (!ACCOUNT_PRINCIPAL_KEY.test(principalKey)) {
    throw new TypeError('invalid_trace_principal_key')
  }

  const digest = createHash('sha256').update(`local-trace-principal-v1\u0000${principalKey}`, 'utf8').digest('hex')

  return legacyTraceOwner(digest, installationId)
}

export function previousLegacyTraceAccountKey(scope: ConnectionScope, installationId: string): string {
  const digest = createHash('sha256')
    .update(
      `legacy-trace-principal-v1\u0000${scope.connection_id}\u0000${scope.runtime_instance_id}\u0000${scope.epoch}\u0000${installationId}`,
      'utf8'
    )
    .digest('hex')

  return legacyTraceOwner(digest, installationId).accountKey
}

export async function migratePreviousLegacyTraceNamespace(options: LegacyTraceMigrationOptions): Promise<void> {
  if (options.previousAccountKey === options.currentAccountKey) {
    return
  }

  try {
    await options.rename(join(options.root, options.previousAccountKey), join(options.root, options.currentAccountKey))
  } catch (error) {
    const code = error instanceof Error && 'code' in error ? String(error.code) : ''

    if (code !== 'ENOENT' && code !== 'EEXIST' && code !== 'ENOTEMPTY') {
      throw error
    }
  }
}
