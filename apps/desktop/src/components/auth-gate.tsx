import {
  createContext,
  type FormEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react'

import type { DesktopBootstrapEvent, DesktopBootstrapState } from '@/global'
import { type Translations, useI18n } from '@/i18n'

const ACCOUNT_SERVER = 'https://c2sml.cn/agent'
const AUTH_REQUEST_TIMEOUT_MS = 15_000

function withAuthDeadline<T>(request: Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = globalThis.setTimeout(() => reject(new Error('auth_request_timeout')), AUTH_REQUEST_TIMEOUT_MS)

    request.then(
      value => {
        globalThis.clearTimeout(timer)
        resolve(value)
      },
      error => {
        globalThis.clearTimeout(timer)
        reject(error)
      }
    )
  })
}

export type DesktopAccountStatus = {
  state: 'checking' | 'authenticated' | 'signed_out' | 'locked'
  username: string | null
  runtime_instance_id: string
  epoch: number
  valid_until: number
  session_expires_at: string | null
  reason: string | null
}

export type DesktopAuthClient = {
  status: (connectionId?: string) => Promise<DesktopAccountStatus>
  login: (username: string, password: string, connectionId?: string) => Promise<DesktopAccountStatus>
  logout: (connectionId?: string) => Promise<DesktopAccountStatus>
  onChanged: (callback: (status: DesktopAccountStatus, connectionId?: string) => void) => () => void
}

type DesktopBootstrapClient = {
  getBootstrapState: () => Promise<DesktopBootstrapState>
  onBootstrapEvent: (callback: (event: DesktopBootstrapEvent) => void) => () => void
  resetBootstrap?: () => Promise<{ ok: boolean }>
}

export type DesktopAuthContextValue = {
  connectionId: string
  logout: () => Promise<DesktopAccountStatus>
  status: DesktopAccountStatus
}

const DesktopAuthContext = createContext<DesktopAuthContextValue | null>(null)

export function useDesktopAuth(): DesktopAuthContextValue {
  const value = useContext(DesktopAuthContext)

  if (!value) {
    throw new Error('Desktop auth context is only available after authentication')
  }

  return value
}

const unavailableStatus = (): DesktopAccountStatus => ({
  state: 'locked',
  username: null,
  runtime_instance_id: 'unavailable',
  epoch: 0,
  valid_until: 0,
  session_expires_at: null,
  reason: 'runtime_unavailable'
})

const emptyBootstrapState = (): DesktopBootstrapState => ({
  active: false,
  manifest: null,
  stages: {},
  error: null,
  log: [],
  startedAt: null,
  completedAt: null,
  setupChoice: null,
  unsupportedPlatform: null
})

function applyAuthBootstrapEvent(
  state: DesktopBootstrapState | null,
  event: DesktopBootstrapEvent
): DesktopBootstrapState | null {
  const current = state || emptyBootstrapState()

  if (event.type === 'dismissed') {
    return null
  }

  if (event.type === 'manifest') {
    return {
      ...current,
      active: true,
      error: null,
      manifest: event,
      stages: Object.fromEntries(
        event.stages.map(stage => [
          stage.name,
          { state: 'pending', durationMs: null, startedAt: null, json: null, error: null }
        ])
      ),
      startedAt: current.startedAt || Date.now()
    }
  }

  if (event.type === 'stage') {
    const previous = current.stages[event.name]

    return {
      ...current,
      stages: {
        ...current.stages,
        [event.name]: {
          state: event.state,
          durationMs: event.durationMs ?? null,
          startedAt: event.state === 'running' ? (previous?.startedAt ?? Date.now()) : (previous?.startedAt ?? null),
          json: event.json ?? null,
          // Raw installer errors never cross the pre-auth rendering surface.
          error: event.state === 'failed' ? 'bootstrap_failed' : null
        }
      }
    }
  }

  if (event.type === 'complete') {
    return { ...current, active: false, error: null, completedAt: Date.now() }
  }

  if (event.type === 'failed' || event.type === 'unsupported-platform') {
    return { ...current, active: false, error: 'bootstrap_failed' }
  }

  return current
}

function currentBootstrapStage(state: DesktopBootstrapState | null) {
  const descriptors = state?.manifest?.stages || []
  const index = descriptors.findIndex(descriptor => state?.stages[descriptor.name]?.state === 'running')

  if (index < 0) {
    return null
  }

  const descriptor = descriptors[index]

  const title = String(descriptor.title || descriptor.name)
    .replace(/[^/\p{L}\p{N} ._()-]/gu, '')
    .slice(0, 80)

  return { current: index + 1, title, total: descriptors.length }
}

export function AuthGate({
  auth = window.hermesDesktop?.auth,
  bootstrap = window.hermesDesktop,
  children
}: {
  auth?: DesktopAuthClient
  bootstrap?: DesktopBootstrapClient
  children: ReactNode
  // Kept as a defensive compatibility surface: unauthenticated content is
  // never rendered outside the fixed account login gate.
  unauthenticatedOverlay?: ReactNode
}) {
  const { t } = useI18n()

  const [status, setStatus] = useState<DesktopAccountStatus>({
    ...unavailableStatus(),
    state: 'checking',
    reason: null
  })

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [connectionId, setConnectionId] = useState('local')
  const [bootstrapState, setBootstrapState] = useState<DesktopBootstrapState | null>(null)
  const eventRevision = useRef(0)
  const requestRevision = useRef(0)
  const bootstrapEventRevision = useRef(0)

  // eslint-disable-next-line no-restricted-syntax -- revision refs are imperative request-order tokens, not atom mirrors
  useEffect(() => {
    if (!auth) {
      requestRevision.current += 1
      setStatus(unavailableStatus())

      return
    }

    let active = true
    const request = ++requestRevision.current
    const observedEvent = eventRevision.current

    const unsubscribe = auth.onChanged((next, nextConnectionId = 'local') => {
      if (active) {
        eventRevision.current += 1
        requestRevision.current += 1
        setConnectionId(nextConnectionId)
        setStatus(next)
      }
    })

    void withAuthDeadline(auth.status(connectionId === 'local' ? undefined : connectionId))
      .then(next => {
        if (active && request === requestRevision.current && observedEvent === eventRevision.current) {
          setStatus(next)
        }
      })
      .catch(() => {
        if (active && request === requestRevision.current && observedEvent === eventRevision.current) {
          setStatus(unavailableStatus())
        }
      })

    return () => {
      active = false
      requestRevision.current += 1
      unsubscribe()
    }
  }, [auth, connectionId])

  // eslint-disable-next-line no-restricted-syntax -- revision refs order bootstrap snapshot/event delivery
  useEffect(() => {
    if (!bootstrap) {
      return
    }

    let active = true
    const observedEvent = bootstrapEventRevision.current
    void bootstrap
      .getBootstrapState()
      .then(snapshot => {
        if (active && observedEvent === bootstrapEventRevision.current) {
          setBootstrapState(snapshot)
        }
      })
      .catch(() => {
        // The bounded auth request remains authoritative when no snapshot is available.
      })

    const unsubscribe = bootstrap.onBootstrapEvent(event => {
      if (!active) {
        return
      }

      bootstrapEventRevision.current += 1
      setBootstrapState(current => applyAuthBootstrapEvent(current, event))

      if (event.type === 'failed' || event.type === 'unsupported-platform') {
        requestRevision.current += 1
        setStatus(current => (current.state === 'checking' ? unavailableStatus() : current))
      }
    })

    return () => {
      active = false
      unsubscribe()
    }
  }, [bootstrap])

  const logout = useCallback(() => {
    if (!auth) {
      return Promise.resolve(unavailableStatus())
    }

    return auth.logout(connectionId === 'local' ? undefined : connectionId)
  }, [auth, connectionId])

  const authenticatedContext = useMemo<DesktopAuthContextValue>(
    () => ({ connectionId, logout, status }),
    [connectionId, logout, status]
  )

  if (status.state === 'authenticated') {
    return <DesktopAuthContext.Provider value={authenticatedContext}>{children}</DesktopAuthContext.Provider>
  }

  const retry = () => {
    if (!auth || submitting) {
      return
    }

    const request = ++requestRevision.current
    const observedEvent = eventRevision.current
    setStatus(current => ({ ...current, state: 'checking', reason: null }))
    const reset = bootstrapState?.error && bootstrap?.resetBootstrap ? bootstrap.resetBootstrap() : Promise.resolve()

    void reset
      .then(() => {
        setBootstrapState(current => (current ? { ...current, error: null } : current))

        return withAuthDeadline(auth.status(connectionId === 'local' ? undefined : connectionId))
      })
      .then(next => {
        if (request === requestRevision.current && observedEvent === eventRevision.current) {
          setStatus(next)
        }
      })
      .catch(() => {
        if (request === requestRevision.current && observedEvent === eventRevision.current) {
          setStatus(unavailableStatus())
        }
      })
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()

    if (!auth || submitting || !username.trim() || !password) {
      return
    }

    setSubmitting(true)
    const request = ++requestRevision.current
    const observedEvent = eventRevision.current
    void withAuthDeadline(auth.login(username.trim(), password, ...(connectionId === 'local' ? [] : [connectionId])))
      .then(next => {
        if (request === requestRevision.current && observedEvent === eventRevision.current) {
          setStatus(next)
        }
      })
      .catch(() => {
        if (request === requestRevision.current && observedEvent === eventRevision.current) {
          setStatus(unavailableStatus())
        }
      })
      .finally(() => {
        setPassword('')
        setSubmitting(false)
      })
  }

  const reason = reasonText(t.auth.reasons, status.reason)
  const bootstrapStage = currentBootstrapStage(bootstrapState)

  const checkingText = bootstrapState?.active
    ? bootstrapStage
      ? t.auth.preparingStage(bootstrapStage.current, bootstrapStage.total, bootstrapStage.title)
      : t.auth.preparingRuntime
    : t.auth.checking

  return (
    <main className="fixed inset-0 grid min-h-screen place-items-center bg-(--ui-chat-surface-background) p-6 text-(--dt-foreground)">
      <section
        aria-busy={status.state === 'checking'}
        className="w-full max-w-md rounded-2xl border border-(--dt-border) bg-(--ui-card-surface) p-8 shadow-2xl"
      >
        <div aria-hidden="true" className="mb-6 text-2xl font-semibold tracking-tight">
          Hermes
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">{t.auth.title}</h1>
        <p className="mt-2 text-sm leading-6 text-(--dt-muted-foreground)">{t.auth.description}</p>

        <div className="mt-5 rounded-lg border border-(--dt-border) bg-(--ui-sidebar-surface) px-4 py-3">
          <div className="text-xs font-medium uppercase tracking-wide text-(--dt-muted-foreground)">
            {t.auth.serverLabel}
          </div>
          <code className="mt-1 block break-all text-sm">{ACCOUNT_SERVER}</code>
        </div>

        {status.state === 'checking' ? (
          <p className="mt-6 text-sm" role="status">
            {checkingText}
          </p>
        ) : (
          <form className="mt-6 space-y-4" onSubmit={submit}>
            <label className="block text-sm font-medium">
              <span>{t.auth.username}</span>
              <input
                autoComplete="username"
                className="mt-2 w-full rounded-lg border border-(--dt-input) bg-transparent px-3 py-2 outline-none focus:border-(--dt-ring)"
                maxLength={150}
                name="username"
                onChange={event => setUsername(event.target.value)}
                required
                value={username}
              />
            </label>
            <label className="block text-sm font-medium">
              <span>{t.auth.password}</span>
              <input
                autoComplete="current-password"
                className="mt-2 w-full rounded-lg border border-(--dt-input) bg-transparent px-3 py-2 outline-none focus:border-(--dt-ring)"
                maxLength={4096}
                name="password"
                onChange={event => setPassword(event.target.value)}
                required
                type="password"
                value={password}
              />
            </label>

            {reason ? (
              <p className="text-sm text-(--dt-destructive)" role="alert">
                {reason}
              </p>
            ) : null}

            <button
              className="w-full rounded-lg bg-(--dt-primary) px-4 py-2.5 font-semibold text-(--dt-primary-foreground) disabled:opacity-50"
              disabled={submitting || !username.trim() || !password}
              type="submit"
            >
              {submitting ? t.auth.signingIn : t.auth.signIn}
            </button>
          </form>
        )}

        <button
          className="mt-4 text-sm font-medium text-(--dt-muted-foreground) underline underline-offset-4 disabled:opacity-50"
          disabled={submitting || status.state === 'checking'}
          onClick={retry}
          type="button"
        >
          {t.auth.retry}
        </button>
        <p className="mt-6 border-t border-(--dt-border) pt-4 text-xs leading-5 text-(--dt-muted-foreground)">
          {t.auth.administratorManaged}
        </p>
      </section>
    </main>
  )
}

function reasonText(reasons: Translations['auth']['reasons'], reason: string | null) {
  switch (reason) {
    case 'invalid_credentials':
      return reasons.invalidCredentials

    case 'rate_limited':
      return reasons.rateLimited

    case 'runtime_unavailable':
      return reasons.runtimeUnavailable

    case 'server_unavailable':
      return reasons.serverUnavailable

    case 'session_expired':
      return reasons.sessionExpired

    case 'session_rejected':
      return reasons.sessionRejected

    case 'signed_out':
      return reasons.signedOut

    case 'vault_unavailable':
      return reasons.vaultUnavailable

    default:
      return reasons.interactiveLoginRequired
  }
}
