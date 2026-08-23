import { type ConnectionScope, requireAuthenticatedConnectionScope } from './auth-bridge'

interface SessionListResponse {
  sessions: unknown[]
  total: number
  [key: string]: unknown
}

export interface ProfileSessionsResponse extends SessionListResponse {
  profile_totals: Record<string, number>
}

type FetchJsonForProfile = (profile: string | null, path: string) => Promise<unknown>

export interface ConnectionProfileTarget {
  connectionId: string
  profile: string
}

interface ConnectionProfileSessionsDependencies {
  fetchJson: (connectionId: string, profile: string, path: string, bearer: string) => Promise<unknown>
  requestFreshToken: (scope: ConnectionScope) => Promise<string>
  requireScope: (connectionId: string) => Promise<ConnectionScope>
}

const REMOTE_SESSION_PAGE_LIMIT = 100

function rowsOf(data: unknown): unknown[] {
  if (!data || typeof data !== 'object' || !('sessions' in data)) {
    return []
  }

  return Array.isArray(data.sessions) ? data.sessions : []
}

function sessionId(row: unknown): string | null {
  if (!row || typeof row !== 'object' || !('id' in row)) {
    return null
  }

  return typeof row.id === 'string' ? row.id : null
}

function nonNegativeNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

function isPinned(row: unknown): boolean {
  return Boolean(row && typeof row === 'object' && 'pinned' in row && row.pinned)
}

function profileSessionId(row: unknown): string | null {
  const id = sessionId(row)

  if (!id) {
    return null
  }

  const profile =
    row && typeof row === 'object' && 'profile' in row && typeof row.profile === 'string' ? row.profile : ''

  return `${profile}\0${id}`
}

export function mergeProfileSessionWindow(rows: unknown[], offset: number, limit: number): unknown[] {
  const window = rows.slice(offset, offset + limit)
  const seenRows = new Set(window)
  const seenIds = new Set(window.map(profileSessionId).filter((id): id is string => id !== null))

  for (const row of rows.slice(offset + limit)) {
    if (!isPinned(row)) {
      continue
    }

    const id = profileSessionId(row)

    if ((id && seenIds.has(id)) || (!id && seenRows.has(row))) {
      continue
    }

    if (id) {
      seenIds.add(id)
    } else {
      seenRows.add(row)
    }

    window.push(row)
  }

  return window
}

export async function fetchPrimaryProfileSessions(
  searchParams: URLSearchParams,
  fetchJsonForProfile: FetchJsonForProfile
): Promise<ProfileSessionsResponse> {
  try {
    return (await fetchJsonForProfile(null, `/api/profiles/sessions?${searchParams}`)) as ProfileSessionsResponse
  } catch {
    return { sessions: [], total: 0, profile_totals: {} }
  }
}

export async function fetchRemoteProfileSessions(
  profile: string,
  searchParams: URLSearchParams,
  fetchJsonForProfile: FetchJsonForProfile
): Promise<SessionListResponse> {
  const params = new URLSearchParams(searchParams)
  params.delete('profile') // the remote serves its own database

  const requestedLimit = Number(params.get('limit'))
  const requestedOffset = Number(params.get('offset') || '0')

  const needsPaging =
    Number.isInteger(requestedLimit) &&
    requestedLimit > REMOTE_SESSION_PAGE_LIMIT &&
    Number.isInteger(requestedOffset) &&
    requestedOffset >= 0

  if (!needsPaging) {
    return (await fetchJsonForProfile(profile, `/api/sessions?${params}`)) as SessionListResponse
  }

  const sessions: unknown[] = []
  const backfilled: unknown[] = []
  const seenIds = new Set<string>()
  const backfilledIds = new Set<string>()
  let firstPage: SessionListResponse | null = null
  let pageOffset = requestedOffset
  let targetOffset = requestedOffset + requestedLimit

  while (pageOffset < targetOffset) {
    const pageParams = new URLSearchParams(params)
    const pageLimit = Math.min(REMOTE_SESSION_PAGE_LIMIT, targetOffset - pageOffset)
    pageParams.set('limit', String(pageLimit))
    pageParams.set('offset', String(pageOffset))

    const page = (await fetchJsonForProfile(profile, `/api/sessions?${pageParams}`)) as SessionListResponse
    firstPage ??= page

    const total = nonNegativeNumber(page.total)
    const pageRows = rowsOf(page)

    const windowedCount =
      total !== null ? Math.min(pageLimit, Math.max(0, total - pageOffset)) : Math.min(pageLimit, pageRows.length)

    // /api/sessions appends pinned rows that fall outside the requested
    // window. Keep those aside until all ordinary pages have been joined so
    // pagination preserves the same order as one larger request.
    for (const row of pageRows.slice(0, windowedCount)) {
      const id = sessionId(row)

      if (id && seenIds.has(id)) {
        continue
      }

      if (id) {
        seenIds.add(id)
        backfilledIds.delete(id)
      }

      sessions.push(row)
    }

    for (const row of pageRows.slice(windowedCount)) {
      const id = sessionId(row)

      if ((id && seenIds.has(id)) || (id && backfilledIds.has(id))) {
        continue
      }

      if (id) {
        backfilledIds.add(id)
      }

      backfilled.push(row)
    }

    if (total !== null) {
      targetOffset = Math.min(targetOffset, total)
    }

    pageOffset += pageLimit
  }

  for (const row of backfilled) {
    const id = sessionId(row)

    if (!id || backfilledIds.has(id)) {
      sessions.push(row)
    }
  }

  const total = nonNegativeNumber(firstPage?.total)

  return {
    ...(firstPage || {}),
    sessions,
    total: total ?? sessions.length,
    limit: requestedLimit,
    offset: requestedOffset
  }
}

export async function fetchConnectionProfileSessions(
  target: ConnectionProfileTarget,
  searchParams: URLSearchParams,
  dependencies: ConnectionProfileSessionsDependencies
): Promise<SessionListResponse> {
  const connectionId = String(target.connectionId || '').trim()
  const profile = String(target.profile || '').trim() || 'default'

  if (!connectionId) {
    throw new Error('AUTH_REQUIRED')
  }

  return fetchRemoteProfileSessions(profile, searchParams, async (_profile, path) => {
    let scope: ConnectionScope

    try {
      scope = requireAuthenticatedConnectionScope(await dependencies.requireScope(connectionId))
    } catch {
      throw new Error('AUTH_REQUIRED')
    }

    if (scope.connection_id !== connectionId) {
      throw new Error('AUTH_REQUIRED')
    }

    const bearer = await dependencies.requestFreshToken(scope)

    if (!bearer) {
      throw new Error('AUTH_REQUIRED')
    }

    return dependencies.fetchJson(connectionId, profile, path, bearer)
  })
}
