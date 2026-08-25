import { createHash, randomUUID } from 'node:crypto'
import { mkdir, open, readFile, rename, rm } from 'node:fs/promises'
import { dirname, join } from 'node:path'

import type { ConnectionScope } from './auth-bridge'
import { isCanonicalUuidV4, type TraceOwner } from './trace-outbox-types'
import { legacyTraceOwner } from './trace-recovery-controller'

const LEGACY_PRINCIPAL_KEY = /^legacy:([0-9a-f]{64})$/
const ACCOUNT_PRINCIPAL_KEY = /^account:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

type LegacyTraceMigrationOptions = {
  currentAccountKey: string
  previousAccountKey: string
  rename: (source: string, destination: string) => Promise<void>
  root: string
}

type TraceAuthorizationStatus = {
  account_id: string | null
  epoch: number
  installation_id: string | null
  legacy: boolean
  principal_key: string | null
  runtime_instance_id: string
  session_id: string | null
  state: string
  username?: string | null
}

export type TraceNamespaceTransition = {
  installationId: string
  sourceAccountKey: string
  targetAccountKey: string
}

const TRACE_TRANSITION_FILE = 'trusted-migration.json'

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

export function traceOwnerFromScope(
  status: TraceAuthorizationStatus,
  scope: ConnectionScope,
  installationId: string
): TraceOwner {
  const nativePrincipal =
    typeof status.principal_key === 'string' ? ACCOUNT_PRINCIPAL_KEY.exec(status.principal_key) : null
  const exactNative =
    status.state === 'authenticated' &&
    status.legacy === false &&
    status.runtime_instance_id === scope.runtime_instance_id &&
    status.epoch === scope.epoch &&
    status.installation_id === installationId &&
    isCanonicalUuidV4(status.account_id) &&
    isCanonicalUuidV4(status.session_id) &&
    nativePrincipal !== null &&
    status.principal_key === `account:${status.account_id}`

  if (exactNative) {
    return {
      accountId: status.account_id,
      accountKey: `account-${status.account_id}`,
      installationId,
      sessionId: status.session_id
    }
  }

  if (status.principal_key !== null) {
    return localOnlyTraceOwnerForPrincipal(status.principal_key, installationId)
  }

  const digest = createHash('sha256')
    .update(`local-trace-missing-principal-v1\u0000${scope.connection_id}\u0000${installationId}`, 'utf8')
    .digest('hex')

  return legacyTraceOwner(digest, installationId)
}

export function traceNamespaceTransition(
  previous: TraceAuthorizationStatus | null,
  current: TraceAuthorizationStatus,
  scope: ConnectionScope,
  installationId: string
): TraceNamespaceTransition | null {
  if (previous?.state !== 'authenticated' || previous.legacy !== true || previous.principal_key === null) {
    return null
  }

  let source: TraceOwner
  try {
    source = legacyTraceOwnerForPrincipal(previous.principal_key, installationId)
  } catch {
    return null
  }
  const target = traceOwnerFromScope(current, scope, installationId)

  if (!target.accountKey.startsWith('account-')) {
    return null
  }

  return {
    installationId,
    sourceAccountKey: source.accountKey,
    targetAccountKey: target.accountKey
  }
}

export async function writeTraceNamespaceTransition(root: string, transition: TraceNamespaceTransition): Promise<void> {
  const target = join(root, TRACE_TRANSITION_FILE)
  const temporary = `${target}.tmp-${randomUUID()}`
  await mkdir(root, { mode: 0o700, recursive: true })
  const handle = await open(temporary, 'wx', 0o600)
  try {
    await handle.writeFile(JSON.stringify({ version: 1, ...transition }), 'utf8')
    await handle.sync()
  } finally {
    await handle.close()
  }
  await rename(temporary, target)
  const directory = await open(dirname(target), 'r')
  try {
    await directory.sync()
  } finally {
    await directory.close()
  }
}

export async function readTraceNamespaceTransition(
  root: string,
  targetOwner: TraceOwner
): Promise<TraceNamespaceTransition | null> {
  try {
    const parsed = JSON.parse(await readFile(join(root, TRACE_TRANSITION_FILE), 'utf8')) as Record<string, unknown>
    if (
      Object.keys(parsed).sort().join(',') !== 'installationId,sourceAccountKey,targetAccountKey,version' ||
      parsed.version !== 1 ||
      parsed.installationId !== targetOwner.installationId ||
      parsed.targetAccountKey !== targetOwner.accountKey ||
      typeof parsed.sourceAccountKey !== 'string' ||
      !/^legacy-[0-9a-f]{64}$/.test(parsed.sourceAccountKey)
    ) {
      return null
    }

    return {
      installationId: parsed.installationId,
      sourceAccountKey: parsed.sourceAccountKey,
      targetAccountKey: parsed.targetAccountKey
    } as TraceNamespaceTransition
  } catch {
    return null
  }
}

export async function clearTraceNamespaceTransition(root: string): Promise<void> {
  await rm(join(root, TRACE_TRANSITION_FILE), { force: true })
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
