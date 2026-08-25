import { createHash } from 'node:crypto'
import { join } from 'node:path'

import type { ConnectionScope } from './auth-bridge'
import type { TraceOwner } from './trace-outbox-types'
import { legacyTraceOwner } from './trace-recovery-controller'

const LEGACY_PRINCIPAL_KEY = /^legacy:([0-9a-f]{64})$/

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
