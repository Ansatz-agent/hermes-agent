import { randomBytes as nodeRandomBytes } from 'node:crypto'
import { uptime } from 'node:os'

import { ansatzAuthEnvironment } from './ansatz-product'
import { type ConnectionScope, requireAuthenticatedConnectionScope } from './auth-bridge'

export const DESKTOP_SCOPE_PROTOCOL_VERSION = 2
export const AUTH_SCOPE_TOKEN_TTL_SECONDS = 1_800
export const AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS = 1_200
export const AUTH_SCOPE_TOKEN_OVERLAP_SECONDS = 60

const AUTH_SCOPE_TOKEN_BYTES = 32
const AUTH_SCOPE_ID_BYTES = 16
const AUTH_SCOPE_CONTROL_FRAME_MAX_BYTES = 4_096

const AUTH_SECRET_ENVIRONMENT_KEYS = new Set(['HERMES_AUTH_SCOPE_TOKEN', 'HERMES_DASHBOARD_SESSION_TOKEN'])

export type AuthScopeToken = {
  bearer: string
  registrationId: string
  scope: ConnectionScope
  issuedAt: number
  rotateAt: number
  ttlSeconds: number
  validUntil: number
}

export type ScopeTokenRegisteredAck = {
  version: 2
  operation: 'scope_token_registered'
  registration_id: string
  connection_id: string
  runtime_instance_id: string
  epoch: number
  ttl_seconds: number
}

export type ScopeTokenPromotedAck = {
  version: 2
  operation: 'scope_token_promoted'
  transition_id: string
  registration_id: string
  previous_registration_id: string | null
  connection_id: string
  runtime_instance_id: string
  epoch: number
  overlap_seconds: number
}

export type ScopeControlAck = ScopeTokenRegisteredAck | ScopeTokenPromotedAck

export type TraceTransportRegistration = {
  endpoint: string
  installationId: string
  localBearer: string
  pluginsToml: string
}

export function sanitizeAuthChildEnvironment(source: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const sanitized = { ...source }

  for (const key of AUTH_SECRET_ENVIRONMENT_KEYS) {
    delete sanitized[key]
  }

  return sanitized
}

export function sanitizeAnsatzAuthChildEnvironment(source: NodeJS.ProcessEnv, hermesHome: string): NodeJS.ProcessEnv {
  return sanitizeAuthChildEnvironment(ansatzAuthEnvironment(hermesHome, source))
}

type IssueAuthScopeTokenOptions = {
  clock?: () => number
  randomBytes?: (size: number) => Buffer
  randomIdBytes?: (size: number) => Buffer
}

export function issueAuthScopeToken(scope: ConnectionScope, options: IssueAuthScopeTokenOptions = {}): AuthScopeToken {
  let required: ConnectionScope

  try {
    required = requireAuthenticatedConnectionScope(scope)
  } catch {
    throw new TypeError('Invalid auth scope')
  }

  const clock = options.clock ?? uptime
  const bearerSource = options.randomBytes ?? nodeRandomBytes
  const idSource = options.randomIdBytes ?? nodeRandomBytes
  const issuedAt = clock()
  const bearer = bearerSource(AUTH_SCOPE_TOKEN_BYTES).toString('base64url')
  const registrationId = idSource(AUTH_SCOPE_ID_BYTES).toString('base64url')

  if (Buffer.from(bearer, 'base64url').byteLength !== AUTH_SCOPE_TOKEN_BYTES) {
    throw new Error('Auth scope token entropy source returned an invalid value')
  }
  if (Buffer.from(registrationId, 'base64url').byteLength !== AUTH_SCOPE_ID_BYTES) {
    throw new Error('Auth scope registration id source returned an invalid value')
  }

  return {
    bearer,
    registrationId,
    scope: { ...required },
    issuedAt,
    rotateAt: issuedAt + AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS,
    ttlSeconds: AUTH_SCOPE_TOKEN_TTL_SECONDS,
    validUntil: issuedAt + AUTH_SCOPE_TOKEN_TTL_SECONDS
  }
}

export function issueScopeTransitionId(randomBytes = nodeRandomBytes): string {
  const transitionId = randomBytes(AUTH_SCOPE_ID_BYTES).toString('base64url')
  if (Buffer.from(transitionId, 'base64url').byteLength !== AUTH_SCOPE_ID_BYTES) {
    throw new Error('Auth scope transition id source returned an invalid value')
  }
  return transitionId
}

export function encodeScopeTokenRegistration(token: AuthScopeToken): string {
  return boundedScopeControlFrame({
    version: DESKTOP_SCOPE_PROTOCOL_VERSION,
    operation: 'register_scope_token',
    registration_id: token.registrationId,
    bearer: token.bearer,
    connection_id: token.scope.connection_id,
    runtime_instance_id: token.scope.runtime_instance_id,
    epoch: token.scope.epoch,
    ttl_seconds: token.ttlSeconds
  })
}

export function encodeScopeTokenPromotion(
  token: AuthScopeToken,
  previousRegistrationId: string | null,
  transitionId: string
): string {
  return boundedScopeControlFrame({
    version: DESKTOP_SCOPE_PROTOCOL_VERSION,
    operation: 'promote_scope_token',
    transition_id: transitionId,
    registration_id: token.registrationId,
    previous_registration_id: previousRegistrationId,
    connection_id: token.scope.connection_id,
    runtime_instance_id: token.scope.runtime_instance_id,
    epoch: token.scope.epoch,
    overlap_seconds: AUTH_SCOPE_TOKEN_OVERLAP_SECONDS
  })
}

function boundedScopeControlFrame(value: Record<string, unknown>): string {
  const frame = `${JSON.stringify(value)}\n`
  if (Buffer.byteLength(frame) > AUTH_SCOPE_CONTROL_FRAME_MAX_BYTES) {
    throw new Error('Auth scope control frame is too large')
  }
  return frame
}

export function encodeTraceTransportRegistration(transport: TraceTransportRegistration): string {
  const endpoint = new URL(transport.endpoint)

  if (
    endpoint.protocol !== 'http:' ||
    endpoint.hostname !== '127.0.0.1' ||
    endpoint.pathname !== '/v1/traces' ||
    !endpoint.port ||
    endpoint.search ||
    endpoint.hash ||
    !/^[0-9A-Za-z_-]{43}$/.test(transport.localBearer) ||
    Buffer.from(transport.localBearer, 'base64url').byteLength !== 32 ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(transport.installationId) ||
    !transport.pluginsToml.replaceAll('\\', '/').endsWith('/ansatz-voice-trace/plugins.toml')
  ) {
    throw new TypeError('Invalid Trace transport registration')
  }

  const frame = `${JSON.stringify({
    authorization: `Bearer ${transport.localBearer}`,
    endpoint: transport.endpoint,
    entrypoint: 'desktop',
    installation_id: transport.installationId,
    operation: 'register_trace_transport',
    plugins_toml: transport.pluginsToml,
    version: 1
  })}\n`

  if (Buffer.byteLength(frame) > AUTH_SCOPE_CONTROL_FRAME_MAX_BYTES) {
    throw new Error('Trace transport registration frame is too large')
  }

  return frame
}
