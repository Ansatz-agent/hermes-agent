import { randomBytes as nodeRandomBytes } from 'node:crypto'
import { uptime } from 'node:os'

import { ansatzAuthEnvironment } from './ansatz-product'
import { type ConnectionScope, requireAuthenticatedConnectionScope } from './auth-bridge'

export const AUTH_SCOPE_TOKEN_TTL_SECONDS = 60

const AUTH_SCOPE_TOKEN_BYTES = 32
const AUTH_SCOPE_CONTROL_FRAME_MAX_BYTES = 4_096

const AUTH_SECRET_ENVIRONMENT_KEYS = new Set([
  'HERMES_AUTH_SCOPE_TOKEN',
  'HERMES_DASHBOARD_SESSION_TOKEN'
])

export type AuthScopeToken = {
  bearer: string
  scope: ConnectionScope
  ttlSeconds: number
  validUntil: number
}

export function sanitizeAuthChildEnvironment(
  source: NodeJS.ProcessEnv = process.env
): NodeJS.ProcessEnv {
  const sanitized = { ...source }

  for (const key of AUTH_SECRET_ENVIRONMENT_KEYS) {
    delete sanitized[key]
  }

  return sanitized
}

export function sanitizeAnsatzAuthChildEnvironment(
  source: NodeJS.ProcessEnv,
  hermesHome: string
): NodeJS.ProcessEnv {
  return sanitizeAuthChildEnvironment(ansatzAuthEnvironment(hermesHome, source))
}

type IssueAuthScopeTokenOptions = {
  clock?: () => number
  randomBytes?: (size: number) => Buffer
  ttlSeconds?: number
}

export function issueAuthScopeToken(
  scope: ConnectionScope,
  options: IssueAuthScopeTokenOptions = {}
): AuthScopeToken {
  let required: ConnectionScope

  try {
    required = requireAuthenticatedConnectionScope(scope)
  } catch {
    throw new TypeError('Invalid auth scope')
  }

  const ttlSeconds = options.ttlSeconds ?? AUTH_SCOPE_TOKEN_TTL_SECONDS

  if (!Number.isSafeInteger(ttlSeconds) || ttlSeconds <= 0 || ttlSeconds > AUTH_SCOPE_TOKEN_TTL_SECONDS) {
    throw new RangeError(`Auth scope token TTL must be between 1 and ${AUTH_SCOPE_TOKEN_TTL_SECONDS} seconds`)
  }

  const clock = options.clock ?? uptime
  const randomBytes = options.randomBytes ?? nodeRandomBytes
  const bearer = randomBytes(AUTH_SCOPE_TOKEN_BYTES).toString('base64url')

  if (Buffer.from(bearer, 'base64url').byteLength !== AUTH_SCOPE_TOKEN_BYTES) {
    throw new Error('Auth scope token entropy source returned an invalid value')
  }

  return {
    bearer,
    scope: { ...required },
    ttlSeconds,
    validUntil: clock() + ttlSeconds
  }
}

export function encodeScopeTokenRegistration(token: AuthScopeToken): string {
  const frame = `${JSON.stringify({
    bearer: token.bearer,
    connection_id: token.scope.connection_id,
    epoch: token.scope.epoch,
    operation: 'register_scope_token',
    runtime_instance_id: token.scope.runtime_instance_id,
    ttl_seconds: token.ttlSeconds,
    version: 1
  })}\n`

  if (Buffer.byteLength(frame) > AUTH_SCOPE_CONTROL_FRAME_MAX_BYTES) {
    throw new Error('Auth scope token registration frame is too large')
  }

  return frame
}
