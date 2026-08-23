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

import { AuthBootstrapProgress } from '@/components/auth-bootstrap-progress'
import { Button } from '@/components/ui/button'
import type {
  DesktopBootstrapProgressUnit,
  DesktopSafeBootstrapEvent,
  DesktopSafeBootstrapState
} from '@/global'
import { useViewedInterval } from '@/hooks/use-viewed-interval'
import { type Translations, useI18n } from '@/i18n'
import { sanitizeAuthBootstrapText } from '@/lib/auth-bootstrap-progress'

const ACCOUNT_SERVER = 'https://c2sml.cn/agent'
const AUTH_REQUEST_TIMEOUT_MS = 15_000
const AUTH_LOGIN_TIMEOUT_MS = 90_000

function withAuthDeadline<T>(request: Promise<T>, timeoutMs = AUTH_REQUEST_TIMEOUT_MS): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = globalThis.setTimeout(() => reject(new Error('auth_request_timeout')), timeoutMs)

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
  runtime_ready: boolean
}

export type DesktopAuthClient = {
  status: (connectionId?: string) => Promise<DesktopAccountStatus>
  login: (username: string, password: string, connectionId?: string) => Promise<DesktopAccountStatus>
  logout: (connectionId?: string) => Promise<DesktopAccountStatus>
  onChanged: (callback: (status: DesktopAccountStatus, connectionId?: string) => void) => () => void
}

type DesktopAuthBootstrapClient = {
  getState: () => Promise<DesktopSafeBootstrapState>
  onChanged: (callback: (event: DesktopSafeBootstrapEvent) => void) => () => void
  retry: () => Promise<{ ok: boolean }>
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
  reason: 'runtime_unavailable',
  runtime_ready: false
})

const emptyBootstrapState = (): DesktopSafeBootstrapState => ({
  active: false,
  manifest: null,
  stages: {},
  error: null,
  failedStage: null,
  startedAt: null,
  completedAt: null
})

function applyAuthBootstrapEvent(
  state: DesktopSafeBootstrapState | null,
  event: DesktopSafeBootstrapEvent
): DesktopSafeBootstrapState | null {
  const current = state || emptyBootstrapState()

  if (event.type === 'dismissed') {
    return null
  }

  if (event.type === 'manifest') {
    const stages = event.stages.slice(0, 64).map(stage => ({
      ...stage,
      name: sanitizeAuthBootstrapText(stage.name, 'bootstrap-stage'),
      title: sanitizeAuthBootstrapText(stage.title, stage.name),
      category: sanitizeAuthBootstrapText(stage.category, 'runtime')
    }))

    return {
      ...current,
      active: true,
      error: null,
      failedStage: null,
      manifest: { ...event, stages },
      stages: Object.fromEntries(
        stages.map(stage => [
          stage.name,
          { state: 'pending', durationMs: null, startedAt: null, error: null, progress: null }
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
          error: event.state === 'failed' ? 'bootstrap_failed' : null,
          progress: previous?.progress ?? null
        }
      },
      failedStage: event.state === 'failed' ? event.name : current.failedStage
    }
  }

  if (event.type === 'progress') {
    const previous = current.stages[event.stage]
    const units = new Set<DesktopBootstrapProgressUnit>(['bytes', 'packages', 'items', 'files', 'steps'])
    const completed = Number.isFinite(event.completed) && event.completed >= 0 ? event.completed : 0
    const total = typeof event.total === 'number' && Number.isFinite(event.total) && event.total > 0 ? event.total : null

    if (!previous || !units.has(event.unit)) {
      return current
    }

    return {
      ...current,
      stages: {
        ...current.stages,
        [event.stage]: {
          ...previous,
          progress: {
            stage: event.stage,
            completed,
            total,
            unit: event.unit,
            label: sanitizeAuthBootstrapText(event.label, event.stage),
            updatedAt: typeof event.updatedAt === 'number' ? event.updatedAt : Date.now()
          }
        }
      }
    }
  }

  if (event.type === 'complete') {
    return { ...current, active: false, error: null, completedAt: event.completedAt }
  }

  if (event.type === 'failed') {
    const failedStage = event.stage && current.stages[event.stage] ? event.stage : current.failedStage

    return {
      ...current,
      active: false,
      error: 'bootstrap_failed',
      failedStage,
      stages: failedStage
        ? {
            ...current.stages,
            [failedStage]: {
              ...current.stages[failedStage],
              state: 'failed',
              error: 'bootstrap_failed'
            }
          }
        : current.stages
    }
  }

  return current
}

export function AuthGate({
  auth = window.hermesDesktop?.auth,
  bootstrap = window.hermesDesktop?.authBootstrap,
  children
}: {
  auth?: DesktopAuthClient
  bootstrap?: DesktopAuthBootstrapClient
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
  const [bootstrapState, setBootstrapState] = useState<DesktopSafeBootstrapState | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const eventRevision = useRef(0)
  const requestRevision = useRef(0)
  const bootstrapEventRevision = useRef(0)
  const bootstrapStateRef = useRef<DesktopSafeBootstrapState | null>(null)
  const refreshedCompletionRef = useRef<string | null>(null)

  const commitBootstrapState = useCallback((next: DesktopSafeBootstrapState | null) => {
    bootstrapStateRef.current = next
    setBootstrapState(next)
  }, [])

  const requestStatus = useCallback(
    (markChecking = false) => {
      if (!auth) {
        requestRevision.current += 1
        setStatus(unavailableStatus())

        return Promise.resolve(unavailableStatus())
      }

      const request = ++requestRevision.current
      const observedEvent = eventRevision.current

      if (markChecking) {
        setStatus(current => ({ ...current, state: 'checking', reason: null }))
      }

      return withAuthDeadline(auth.status(connectionId === 'local' ? undefined : connectionId))
        .then(next => {
          if (request === requestRevision.current && observedEvent === eventRevision.current) {
            setStatus(next)
          }

          return next
        })
        .catch(() => {
          if (request === requestRevision.current && observedEvent === eventRevision.current) {
            // Bootstrap owns readiness while it is active. Its idle/total
            // deadlines, not this short auth deadline, decide terminal failure.
            if (!bootstrapStateRef.current?.active) {
              setStatus(unavailableStatus())
            }
          }

          return unavailableStatus()
        })
    },
    [auth, connectionId]
  )

  const refreshAfterCompletion = useCallback(
    (state: DesktopSafeBootstrapState) => {
      if (state.completedAt === null) {
        return
      }

      const scope = state.manifest?.bootstrapScope || 'auth'
      const key = `${scope}:${state.startedAt ?? 'unknown'}:${state.completedAt}`

      if (refreshedCompletionRef.current === key) {
        return
      }

      refreshedCompletionRef.current = key
      void requestStatus(scope !== 'runtime')
    },
    [requestStatus]
  )

  // eslint-disable-next-line no-restricted-syntax -- revision refs are imperative request-order tokens, not atom mirrors
  useEffect(() => {
    if (!auth) {
      requestRevision.current += 1
      setStatus(unavailableStatus())

      return
    }

    let active = true

    const unsubscribe = auth.onChanged((next, nextConnectionId = 'local') => {
      if (active) {
        eventRevision.current += 1
        requestRevision.current += 1
        setConnectionId(nextConnectionId)
        setStatus(next)
      }
    })

    void requestStatus()

    return () => {
      active = false
      requestRevision.current += 1
      unsubscribe()
    }
  }, [auth, requestStatus])

  // eslint-disable-next-line no-restricted-syntax -- revision refs order bootstrap snapshot/event delivery
  useEffect(() => {
    if (!bootstrap) {
      return
    }

    let active = true
    const observedEvent = bootstrapEventRevision.current
    void bootstrap
      .getState()
      .then(snapshot => {
        if (active && observedEvent === bootstrapEventRevision.current) {
          commitBootstrapState(snapshot)
          refreshAfterCompletion(snapshot)
        }
      })
      .catch(() => {
        // The bounded auth request remains authoritative when no snapshot is available.
      })

    const unsubscribe = bootstrap.onChanged(event => {
      if (!active) {
        return
      }

      bootstrapEventRevision.current += 1
      const next = applyAuthBootstrapEvent(bootstrapStateRef.current, event)
      commitBootstrapState(next)

      if (next && event.type === 'complete') {
        refreshAfterCompletion(next)
      }
    })

    return () => {
      active = false
      unsubscribe()
    }
  }, [bootstrap, commitBootstrapState, refreshAfterCompletion])

  useViewedInterval(() => setNow(Date.now()), 1_000, Boolean(bootstrapState?.active))

  const logout = useCallback(() => {
    if (!auth) {
      return Promise.resolve(unavailableStatus())
    }

    return auth.logout(connectionId === 'local' ? undefined : connectionId).then(next => {
      requestRevision.current += 1
      setStatus(next)

      return next
    })
  }, [auth, connectionId])

  const authenticatedContext = useMemo<DesktopAuthContextValue>(
    () => ({ connectionId, logout, status }),
    [connectionId, logout, status]
  )

  if (status.state === 'authenticated' && status.runtime_ready) {
    return <DesktopAuthContext.Provider value={authenticatedContext}>{children}</DesktopAuthContext.Provider>
  }

  const retry = () => {
    if (!auth || submitting) {
      return
    }

    if (bootstrapState?.error && bootstrap) {
      commitBootstrapState({ ...bootstrapState, active: true, error: null, failedStage: null })
      void bootstrap
        .retry()
        .then(result => {
          if (!result.ok) {
            throw new Error('bootstrap_retry_failed')
          }

          return requestStatus(bootstrapState.manifest?.bootstrapScope !== 'runtime')
        })
        .catch(() => {
          commitBootstrapState({ ...bootstrapState, active: false, error: 'bootstrap_failed' })
        })

      return
    }

    void requestStatus(true)
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()

    if (!auth || submitting || !username.trim() || !password) {
      return
    }

    setSubmitting(true)
    const request = ++requestRevision.current
    const observedEvent = eventRevision.current

    const loginRequest = auth.login(
      username.trim(),
      password,
      ...(connectionId === 'local' ? [] : [connectionId])
    )

    setPassword('')
    void withAuthDeadline(
      loginRequest,
      AUTH_LOGIN_TIMEOUT_MS
    )
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
        setSubmitting(false)
      })
  }

  const reason = reasonText(t.auth.reasons, status.reason)
  const runtimeSnapshot = bootstrapState?.manifest?.bootstrapScope === 'runtime' ? bootstrapState : null
  const authSnapshot = bootstrapState?.manifest?.bootstrapScope !== 'runtime' ? bootstrapState : null

  const showRuntimeBootstrap = Boolean(
    runtimeSnapshot?.manifest &&
      (runtimeSnapshot.active || runtimeSnapshot.error || runtimeSnapshot.completedAt === null)
  )

  const showAuthBootstrap = Boolean(
    !showRuntimeBootstrap &&
      authSnapshot &&
      (authSnapshot.active || authSnapshot.error || (status.state === 'checking' && authSnapshot.manifest))
  )

  const showBootstrapProgress = showAuthBootstrap || showRuntimeBootstrap

  const showStatusRetry =
    !showBootstrapProgress &&
    status.state !== 'checking' &&
    ['runtime_unavailable', 'server_unavailable', 'vault_unavailable'].includes(status.reason || '')

  return (
    <main className="fixed inset-0 grid min-h-screen place-items-center bg-(--ui-chat-surface-background) p-6 text-(--dt-foreground)">
      <section
        aria-busy={status.state === 'checking' || Boolean(bootstrapState?.active)}
        className="w-full max-w-md rounded-2xl border border-(--dt-border) bg-(--ui-card-surface) p-8 shadow-2xl"
      >
        <div aria-hidden="true" className="mb-6 text-2xl font-semibold tracking-tight">
          Ansatz Voice Trace Client
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {showRuntimeBootstrap ? t.auth.runtimeTitle : t.auth.title}
        </h1>
        <p className="mt-2 text-sm leading-6 text-(--dt-muted-foreground)">
          {showRuntimeBootstrap ? t.auth.runtimeDescription : t.auth.description}
        </p>

        <div className="mt-5 rounded-lg border border-(--dt-border) bg-(--ui-sidebar-surface) px-4 py-3">
          <div className="text-xs font-medium uppercase tracking-wide text-(--dt-muted-foreground)">
            {t.auth.serverLabel}
          </div>
          <code className="mt-1 block break-all text-sm">{ACCOUNT_SERVER}</code>
        </div>
        <p className="mt-3 text-xs leading-5 text-(--dt-muted-foreground)">{t.auth.traceNotice}</p>

        {showAuthBootstrap && authSnapshot ? (
          <AuthBootstrapProgress mode="auth" now={now} onRetry={authSnapshot.error ? retry : undefined} state={authSnapshot} />
        ) : null}

        {showRuntimeBootstrap && runtimeSnapshot ? (
          <AuthBootstrapProgress
            mode="runtime"
            now={now}
            onRetry={runtimeSnapshot.error ? retry : undefined}
            state={runtimeSnapshot}
          />
        ) : null}

        {!showBootstrapProgress && status.state === 'checking' ? (
          <div className="mt-6 space-y-4">
            <p className="text-sm" role="status">
              {t.auth.checking}
            </p>
          </div>
        ) : null}

        {!showBootstrapProgress && status.state !== 'checking' ? (
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

            <Button
              className="w-full"
              disabled={submitting || !username.trim() || !password}
              type="submit"
            >
              {submitting ? t.auth.signingIn : t.auth.signIn}
            </Button>
          </form>
        ) : null}

        {showStatusRetry ? (
          <div className="mt-4">
            <Button disabled={submitting} onClick={retry} size="inline" variant="textStrong">
              {t.auth.retry}
            </Button>
          </div>
        ) : null}

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
