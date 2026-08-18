import { type FormEvent, type ReactNode, useEffect, useRef, useState } from 'react'

import { type Translations, useI18n } from '@/i18n'

const ACCOUNT_SERVER = 'https://c2sml.cn/agent'

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
  status: () => Promise<DesktopAccountStatus>
  login: (username: string, password: string) => Promise<DesktopAccountStatus>
  logout: () => Promise<DesktopAccountStatus>
  onChanged: (callback: (status: DesktopAccountStatus) => void) => () => void
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

export function AuthGate({
  auth = window.hermesDesktop?.auth,
  children,
  unauthenticatedOverlay = null
}: {
  auth?: DesktopAuthClient
  children: ReactNode
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
  const eventRevision = useRef(0)
  const requestRevision = useRef(0)

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

    const unsubscribe = auth.onChanged(next => {
      if (active) {
        eventRevision.current += 1
        requestRevision.current += 1
        setStatus(next)
      }
    })

    void auth
      .status()
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
  }, [auth])

  if (status.state === 'authenticated') {
    return children
  }

  const retry = () => {
    if (!auth || submitting) {
      return
    }

    const request = ++requestRevision.current
    const observedEvent = eventRevision.current
    setStatus(current => ({ ...current, state: 'checking', reason: null }))
    void auth
      .status()
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
    void auth
      .login(username.trim(), password)
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

  return (
    <>
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
              {t.auth.checking}
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
      {unauthenticatedOverlay}
    </>
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
