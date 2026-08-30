import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { type ReactNode, useEffect, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { AuthGate, type DesktopAccountStatus, useDesktopAuth } from './auth-gate'

const signedOut: DesktopAccountStatus = {
  state: 'signed_out',
  username: null,
  account_id: null,
  session_id: null,
  installation_id: null,
  principal_key: null,
  runtime_instance_id: 'runtime-1',
  epoch: 1,
  valid_until: 0,
  cloud_state: null,
  validation_state: 'unknown',
  validation_reason: null,
  last_validated_at: null,
  legacy: false,
  reason: 'signed_out',
  runtime_ready: false
}

const authenticated: DesktopAccountStatus = {
  ...signedOut,
  state: 'authenticated',
  username: 'alice',
  account_id: 'alice',
  session_id: 'session-a',
  principal_key: 'account:alice',
  epoch: 2,
  valid_until: 60,
  cloud_state: 'active',
  reason: null,
  runtime_ready: true
}

function renderGate(
  overrides: Record<string, unknown> = {},
  unauthenticatedOverlay: ReactNode = null,
  protectedChild: ReactNode = <div>Protected Hermes application</div>,
  bootstrap?: Record<string, unknown>
) {
  let changed: ((status: DesktopAccountStatus, connectionId?: string) => void) | null = null

  const auth = {
    status: vi.fn(async () => signedOut),
    login: vi.fn(async () => authenticated),
    logout: vi.fn(async () => signedOut),
    onChanged: vi.fn(callback => {
      changed = callback

      return () => {
        changed = null
      }
    }),
    ...overrides
  }

  const gateProps = { auth, unauthenticatedOverlay, ...(bootstrap ? { bootstrap } : {}) } as any

  render(
    <I18nProvider configClient={null} initialLocale="en">
      <AuthGate {...gateProps}>{protectedChild}</AuthGate>
    </I18nProvider>
  )

  return { auth, emit: (status: DesktopAccountStatus, connectionId?: string) => changed?.(status, connectionId) }
}

function AuthProbe() {
  const { connectionId, logout, status } = useDesktopAuth()

  return (
    <button onClick={() => void logout()} type="button">
      {status.username}:{connectionId}
    </button>
  )
}

function ProtectedMountProbe({ onMount }: { onMount: () => void }) {
  useEffect(() => {
    onMount()
  }, [onMount])

  return <div>Protected Hermes application</div>
}

function StatefulProtectedProbe({ onMount }: { onMount: () => void }) {
  const [count, setCount] = useState(0)

  useEffect(() => {
    onMount()
  }, [onMount])

  return (
    <button onClick={() => setCount(current => current + 1)} type="button">
      Protected Hermes application {count}
    </button>
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('AuthGate', () => {
  it('omits the Trace notice while retaining the administrator footer', async () => {
    renderGate()

    expect(
      await screen.findByText('Accounts and password assistance are managed by the server administrator.')
    ).not.toBeNull()
    expect(
      screen.queryByText(/Complete conversation Traces—including prompts, model replies, and tool arguments\/results/i)
    ).toBeNull()
  })

  it('keeps the approved centered grid layout', async () => {
    renderGate()

    const main = await screen.findByRole('main')
    const card = screen.getByRole('heading', { name: 'Sign in to Ansatz' }).closest('section')

    expect(main.className.split(/\s+/)).toContain('grid')
    expect(main.className.split(/\s+/)).toContain('min-h-screen')
    expect(main.className.split(/\s+/)).toContain('place-items-center')
    expect(main.className.split(/\s+/)).toContain('p-6')
    expect(main.className.split(/\s+/)).not.toContain('overflow-y-auto')
    expect(card?.className.split(/\s+/)).not.toContain('m-auto')
  })

  it('exports the authenticated Desktop auth context hook', async () => {
    const module = await import('./auth-gate')

    expect(module).toHaveProperty('useDesktopAuth')
  })

  it('provides the authenticated username and routes logout to the active connection', async () => {
    const { auth, emit } = renderGate({ status: vi.fn(async () => authenticated) }, null, <AuthProbe />)

    expect(await screen.findByRole('button', { name: 'alice:local' })).not.toBeNull()

    act(() => emit({ ...authenticated, username: 'remote-user' }, 'remote-a'))
    fireEvent.click(await screen.findByRole('button', { name: 'remote-user:remote-a' }))

    await waitFor(() => expect(auth.logout).toHaveBeenCalledWith('remote-a'))
  })

  it('installs the complete runtime before exposing account credentials', async () => {
    let emitBootstrap: ((event: Record<string, unknown>) => void) | null = null

    const status = vi
      .fn()
      .mockImplementationOnce(() => new Promise<DesktopAccountStatus>(() => {}))
      .mockResolvedValueOnce({
        ...signedOut,
        runtime_ready: true
      })

    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          bootstrapScope: 'runtime',
          stages: [{ name: 'complete-runtime', title: 'Install complete offline runtime' }]
        },
        stages: {
          'complete-runtime': {
            state: 'running',
            durationMs: null,
            startedAt: 1_000,
            error: null,
            progress: null
          }
        },
        error: null,
        failedStage: null,
        startedAt: 500,
        completedAt: null
      })),
      onChanged: vi.fn(callback => {
        emitBootstrap = callback

        return () => {
          emitBootstrap = null
        }
      }),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate({ status }, null, <div>Protected Hermes application</div>, bootstrap)

    expect(await screen.findByText('Install complete offline runtime')).not.toBeNull()
    expect(screen.queryByLabelText('Username')).toBeNull()
    expect(screen.queryByLabelText('Password')).toBeNull()

    act(() => emitBootstrap?.({ type: 'complete', completedAt: 2_000 }))

    expect(await screen.findByLabelText('Username')).not.toBeNull()
    expect(screen.getByLabelText('Password')).not.toBeNull()
  })

  it('keeps credentials and protected capabilities hidden during complete runtime installation', async () => {
    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          bootstrapScope: 'runtime',
          stages: [
            {
              name: 'python-deps',
              title: 'Install Python dependencies',
              category: 'runtime',
              needs_user_input: false
            }
          ]
        },
        stages: {
          'python-deps': {
            state: 'running',
            durationMs: null,
            startedAt: Date.now(),
            json: null,
            error: null
          }
        },
        error: null,
        log: [],
        startedAt: Date.now(),
        completedAt: null,
        setupChoice: null,
        unsupportedPlatform: null
      })),
      onChanged: vi.fn(() => () => {}),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate(
      { status: vi.fn(async () => ({ ...authenticated, runtime_ready: false })) },
      null,
      <div>Protected Hermes application</div>,
      bootstrap
    )

    expect(await screen.findByText('0 of 1 stages complete')).not.toBeNull()
    expect(screen.getByText('Install Python dependencies')).not.toBeNull()
    expect(screen.queryByText('Protected Hermes application')).toBeNull()

    expect(screen.queryByLabelText('Username')).toBeNull()
    expect(screen.queryByLabelText('Password')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
  })

  it('resynchronizes the running-stage elapsed time immediately when focus returns', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-20T13:12:50.285Z'))
    let focused = true

    vi.spyOn(globalThis.document, 'hasFocus').mockImplementation(() => focused)

    const startedAt = new Date('2026-08-20T13:12:34.285Z').getTime()

    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          bootstrapScope: 'runtime',
          stages: [{ name: 'node-deps', title: 'Install browser-tool dependencies' }]
        },
        stages: {
          'node-deps': {
            state: 'running',
            durationMs: null,
            startedAt,
            error: null,
            progress: null
          }
        },
        error: null,
        failedStage: null,
        startedAt,
        completedAt: null
      })),
      onChanged: vi.fn(() => () => {}),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate(
      { status: vi.fn(async () => ({ ...authenticated, runtime_ready: false })) },
      null,
      <div>Protected Hermes application</div>,
      bootstrap
    )

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.getByText('Running · 16s')).not.toBeNull()
    focused = false
    act(() => window.dispatchEvent(new Event('blur')))

    vi.setSystemTime(new Date('2026-08-20T13:14:47.285Z'))
    focused = true
    act(() => window.dispatchEvent(new Event('focus')))

    expect(screen.getByText('Running · 2:13')).not.toBeNull()
    expect(screen.queryByText('Protected Hermes application')).toBeNull()
  })

  it('keeps Retry available after pre-auth runtime installation fails', async () => {
    const bootstrap = {
      getState: vi.fn(async () => ({
        active: false,
        manifest: { type: 'manifest', protocolVersion: 1, bootstrapScope: 'runtime', stages: [] },
        stages: {},
        error: 'sessionid=secret Traceback private detail',
        log: [],
        startedAt: Date.now(),
        completedAt: null,
        setupChoice: null,
        unsupportedPlatform: null
      })),
      onChanged: vi.fn(() => () => {}),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate(
      { status: vi.fn(async () => ({ ...authenticated, runtime_ready: false })) },
      null,
      <div>Protected Hermes application</div>,
      bootstrap
    )

    expect(await screen.findByText('Ansatz could not install its local runtime.')).not.toBeNull()
    expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
    expect(globalThis.document.body.textContent).not.toContain('sessionid')
    expect(globalThis.document.body.textContent).not.toContain('Traceback')
  })

  it('shows only the fixed account login surface while signed out', async () => {
    renderGate()

    expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
    expect(screen.getByText('https://c2sml.cn/auth')).not.toBeNull()
    expect(screen.getByLabelText('Username')).not.toBeNull()
    expect(screen.getByLabelText('Password').getAttribute('type')).toBe('password')
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.getAllByText(/server administrator/i).length).toBeGreaterThan(0)
    expect(screen.queryByText('Protected Hermes application')).toBeNull()

    for (const forbidden of ['Register', 'Create account', 'Reset password', 'Change password', 'Skip', 'Offline']) {
      const accessibleName = new RegExp(forbidden, 'i')
      expect(screen.queryByRole('button', { name: accessibleName })).toBeNull()
      expect(screen.queryByRole('link', { name: accessibleName })).toBeNull()
    }

    expect(screen.queryByLabelText(/server/i)).toBeNull()
  })

  it('keeps the password local, clears it, and mounts Hermes only after authentication', async () => {
    const { auth } = renderGate()
    const username = await screen.findByLabelText('Username')
    const password = screen.getByLabelText('Password')

    fireEvent.change(username, { target: { value: 'alice' } })
    fireEvent.change(password, { target: { value: 'password-sentinel' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(auth.login).toHaveBeenCalledWith('alice', 'password-sentinel'))
    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()
    expect(screen.queryByDisplayValue('password-sentinel')).toBeNull()
  })

  it('keeps the protected conversation mounted when validation degrades and recovers', async () => {
    const localAuthorization = authenticated
    const onMount = vi.fn()

    const { emit } = renderGate(
      { status: vi.fn(async () => localAuthorization) },
      null,
      <StatefulProtectedProbe onMount={onMount} />
    )

    const protectedButton = await screen.findByRole('button', { name: 'Protected Hermes application 0' })
    fireEvent.click(protectedButton)
    expect(screen.getByRole('button', { name: 'Protected Hermes application 1' })).not.toBeNull()

    act(() => emit({ ...signedOut, state: 'locked', reason: 'runtime_unavailable', epoch: 3 }))

    expect(screen.getByRole('button', { name: 'Protected Hermes application 1' })).not.toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sign in to Ansatz' })).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()

    act(() =>
      emit({ ...localAuthorization, runtime_ready: false, validation_state: 'online', validation_reason: null })
    )

    expect(screen.getByRole('button', { name: 'Protected Hermes application 1' })).not.toBeNull()
    expect(onMount).toHaveBeenCalledTimes(1)
  })

  it('keeps the protected tree mounted while cloud auth is unreachable', async () => {
    const mounted = vi.fn()
    const { emit } = renderGate(
      { status: vi.fn(async () => authenticated) },
      null,
      <ProtectedMountProbe onMount={mounted} />
    )

    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()
    act(() =>
      emit({
        ...authenticated,
        cloud_state: 'unreachable',
        validation_state: 'degraded',
        validation_reason: 'server_unavailable',
        reason: 'server_unavailable',
        valid_until: 0
      })
    )

    expect(screen.getByText('Protected Hermes application')).not.toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sign in to Ansatz' })).toBeNull()
    expect(mounted).toHaveBeenCalledTimes(1)
  })

  it('keeps the protected conversation mounted when validation times out', async () => {
    vi.useFakeTimers()

    const localAuthorization = authenticated

    const { emit } = renderGate(
      { status: vi.fn(async () => localAuthorization) },
      null,
      <div>Protected Hermes application</div>
    )

    await act(async () => Promise.resolve())
    expect(screen.getByText('Protected Hermes application')).not.toBeNull()

    act(() =>
      emit({
        ...localAuthorization,
        validation_state: 'degraded',
        validation_reason: 'runtime_unavailable'
      })
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })

    expect(screen.getByText('Protected Hermes application')).not.toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sign in to Ansatz' })).toBeNull()
  })

  it.each(['account_disabled', 'account_revoked', 'session_revoked'] as const)(
    'unmounts the protected tree when main emits matching %s',
    async reason => {
      const { emit } = renderGate({ status: vi.fn(async () => authenticated) })
      expect(await screen.findByText('Protected Hermes application')).not.toBeNull()

      act(() => emit({ ...authenticated, state: 'locked', cloud_state: null, reason, epoch: 3 }))

      expect(screen.queryByText('Protected Hermes application')).toBeNull()
      expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
    }
  )

  it.each(['account_disabled', 'account_revoked'] as const)(
    'unmounts the protected tree when matching %s carries an older Session id',
    async reason => {
      const { emit } = renderGate({ status: vi.fn(async () => authenticated) })
      expect(await screen.findByText('Protected Hermes application')).not.toBeNull()

      act(() =>
        emit({ ...authenticated, state: 'locked', cloud_state: null, reason, session_id: 'old-session', epoch: 3 })
      )

      expect(screen.queryByText('Protected Hermes application')).toBeNull()
      expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
    }
  )

  it('keeps the protected tree for a stale session_revoked event from the same account', async () => {
    const { emit } = renderGate({ status: vi.fn(async () => authenticated) })
    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()

    act(() =>
      emit({
        ...authenticated,
        state: 'locked',
        cloud_state: null,
        reason: 'session_revoked',
        session_id: 'old-session',
        epoch: 3
      })
    )

    expect(screen.getByText('Protected Hermes application')).not.toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sign in to Ansatz' })).toBeNull()
  })

  it.each([
    { state: 'locked' as const, reason: 'session_expired' },
    { state: 'locked' as const, reason: 'runtime_unavailable' },
    { state: 'signed_out' as const, reason: 'signed_out' }
  ])(
    'does not retain local authorization after the renderer switches to a remote $state/$reason status',
    async remote => {
      const { emit } = renderGate({ status: vi.fn(async () => authenticated) })
      expect(await screen.findByText('Protected Hermes application')).not.toBeNull()

      act(() => emit({ ...signedOut, ...remote, epoch: 3 }, 'remote-a'))

      expect(screen.queryByText('Protected Hermes application')).toBeNull()
      expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
    }
  )

  it('does not merge a local transient status into an active remote authorization owner', async () => {
    const remoteAuthenticated = {
      ...authenticated,
      username: 'remote-user',
      account_id: 'remote-account',
      session_id: 'remote-session',
      principal_key: 'account:remote-account'
    }

    let localStatus: DesktopAccountStatus = authenticated

    const { emit } = renderGate(
      { status: vi.fn(async connectionId => (connectionId === 'remote-a' ? remoteAuthenticated : localStatus)) },
      null,
      <AuthProbe />
    )

    expect(await screen.findByRole('button', { name: 'alice:local' })).not.toBeNull()

    act(() => emit(remoteAuthenticated, 'remote-a'))
    expect(await screen.findByRole('button', { name: 'remote-user:remote-a' })).not.toBeNull()

    localStatus = { ...signedOut, state: 'locked', reason: 'runtime_unavailable', epoch: 3 }
    act(() => emit(localStatus, 'local'))

    expect(screen.queryByRole('button', { name: 'remote-user:local' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'remote-user:remote-a' })).toBeNull()
    expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
  })

  it('unmounts the protected tree when main emits signed out', async () => {
    const { emit } = renderGate({ status: vi.fn(async () => authenticated) })
    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()

    act(() => emit(signedOut))

    expect(screen.queryByText('Protected Hermes application')).toBeNull()
    expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
  })

  it('remounts the protected scope when the authorized account changes', async () => {
    const alice = { ...authenticated, principal_key: 'account:alice', account_id: 'alice', session_id: 'session-a' }

    const bob = {
      ...authenticated,
      username: 'bob',
      principal_key: 'account:bob',
      account_id: 'bob',
      session_id: 'session-b'
    }

    const onMount = vi.fn()

    const { emit } = renderGate(
      { status: vi.fn(async () => alice) },
      null,
      <StatefulProtectedProbe onMount={onMount} />
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Protected Hermes application 0' }))
    expect(screen.getByRole('button', { name: 'Protected Hermes application 1' })).not.toBeNull()

    act(() => emit(bob))

    expect(screen.getByRole('button', { name: 'Protected Hermes application 0' })).not.toBeNull()
    expect(onMount).toHaveBeenCalledTimes(2)
  })

  it('does not let a late status error regress a transient event', async () => {
    let rejectStatus: ((error: Error) => void) | null = null

    const pendingStatus = new Promise<DesktopAccountStatus>((_resolve, reject) => {
      rejectStatus = reject
    })

    const { emit } = renderGate({ status: vi.fn(() => pendingStatus) }, null, <div>Protected Hermes application</div>)

    act(() => emit(authenticated))
    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()
    act(() => emit({ ...signedOut, state: 'locked', reason: 'server_unavailable', epoch: 3 }))

    await act(async () => {
      rejectStatus?.(new Error('late status failure'))
      await Promise.resolve()
    })

    expect(screen.getByText('Protected Hermes application')).not.toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sign in to Ansatz' })).toBeNull()
  })

  it('turns an unresolved account status request into a terminal retryable state', async () => {
    vi.useFakeTimers()
    renderGate({ status: vi.fn(() => new Promise<DesktopAccountStatus>(() => {})) })

    expect(screen.getByText('Checking your account session…')).not.toBeNull()
    expect(screen.queryByText('Protected Hermes application')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })

    expect(screen.getByText('The secure account service is unavailable. Try again.')).not.toBeNull()
    expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByText('Protected Hermes application')).toBeNull()
  })

  it('keeps an active auth bootstrap visible past 15 seconds without a false Retry', async () => {
    vi.useFakeTimers()

    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          bootstrapScope: 'auth',
          stages: [{ name: 'python-auth-deps', title: 'Install authentication dependencies' }]
        },
        stages: {
          'python-auth-deps': {
            state: 'running',
            durationMs: null,
            startedAt: 1_000,
            error: null,
            progress: {
              stage: 'python-auth-deps',
              completed: 5,
              total: null,
              unit: 'packages',
              label: 'Authentication dependencies',
              updatedAt: 2_000
            }
          }
        },
        error: null,
        failedStage: null,
        startedAt: 500,
        completedAt: null
      })),
      onChanged: vi.fn(() => () => {}),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate(
      { status: vi.fn(() => new Promise<DesktopAccountStatus>(() => {})) },
      null,
      <div>Protected Hermes application</div>,
      bootstrap
    )

    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByText('Preparing the secure sign-in service')).not.toBeNull()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })

    expect(screen.getByText('Preparing the secure sign-in service')).not.toBeNull()
    expect(screen.queryByText('The secure account service is unavailable. Try again.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByText('Protected Hermes application')).toBeNull()
  })

  it('keeps a slow login pending past the short status deadline', async () => {
    vi.useFakeTimers()
    let resolveLogin: ((status: DesktopAccountStatus) => void) | null = null

    const pendingLogin = new Promise<DesktopAccountStatus>(resolve => {
      resolveLogin = resolve
    })

    const { auth } = renderGate({ login: vi.fn(() => pendingLogin) })

    await act(async () => {
      await Promise.resolve()
    })

    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password-sentinel' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(auth.login).toHaveBeenCalledWith('alice', 'password-sentinel')
    expect((screen.getByRole('button', { name: 'Signing in…' }) as HTMLButtonElement).disabled).toBe(true)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })

    expect(screen.queryByText('The secure account service is unavailable. Try again.')).toBeNull()
    expect((screen.getByRole('button', { name: 'Signing in…' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('')

    await act(async () => {
      resolveLogin?.(authenticated)
      await pendingLogin
      await Promise.resolve()
    })

    expect(screen.getByText('Protected Hermes application')).not.toBeNull()
    expect(globalThis.document.body.textContent).not.toContain('password-sentinel')
  })

  it('ends an unresolved login at its longer bounded deadline with no retained password', async () => {
    vi.useFakeTimers()
    renderGate({ login: vi.fn(() => new Promise<DesktopAccountStatus>(() => {})) })
    await act(async () => Promise.resolve())

    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password-sentinel' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(89_999)
    })
    expect(screen.queryByText('The secure account service is unavailable. Try again.')).toBeNull()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(screen.getByText('The secure account service is unavailable. Try again.')).not.toBeNull()
    expect(globalThis.document.body.textContent).not.toContain('password-sentinel')
  })

  it('shows signed bootstrap progress inside the locked login surface', async () => {
    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          stages: [
            {
              name: 'python-auth-deps',
              title: 'Install authentication dependencies',
              category: 'runtime',
              needs_user_input: false
            },
            { name: 'runtime', title: 'Full runtime', category: 'runtime', needs_user_input: false },
            { name: 'gateway', title: 'Gateway', category: 'runtime', needs_user_input: false },
            { name: 'complete', title: 'Complete', category: 'runtime', needs_user_input: false }
          ]
        },
        stages: {
          'python-auth-deps': {
            state: 'running',
            durationMs: null,
            startedAt: Date.now(),
            json: null,
            error: null
          }
        },
        error: null,
        log: [],
        startedAt: Date.now(),
        completedAt: null,
        setupChoice: null,
        unsupportedPlatform: null
      })),
      onChanged: vi.fn(() => () => {}),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate(
      { status: vi.fn(() => new Promise<DesktopAccountStatus>(() => {})) },
      null,
      <div>Protected Hermes application</div>,
      bootstrap
    )

    expect(await screen.findByText('Preparing the secure sign-in service')).not.toBeNull()
    expect(screen.getByText('Stage 1 of 4: Install authentication dependencies')).not.toBeNull()
    expect(screen.queryByText('Protected Hermes application')).toBeNull()
    expect(screen.queryByText('Full runtime')).toBeNull()
  })

  it('refreshes auth status once when bootstrap completes and automatically shows the login form', async () => {
    let emitBootstrap: ((event: Record<string, unknown>) => void) | null = null

    const status = vi
      .fn()
      .mockImplementationOnce(() => new Promise<DesktopAccountStatus>(() => {}))
      .mockResolvedValueOnce(signedOut)

    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          bootstrapScope: 'auth',
          stages: [{ name: 'auth-complete', title: 'Finish authentication runtime' }]
        },
        stages: {
          'auth-complete': {
            state: 'running',
            durationMs: null,
            startedAt: 1_000,
            error: null,
            progress: null
          }
        },
        error: null,
        failedStage: null,
        startedAt: 500,
        completedAt: null
      })),
      onChanged: vi.fn(callback => {
        emitBootstrap = callback

        return () => {
          emitBootstrap = null
        }
      }),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate({ status }, null, <div>Protected Hermes application</div>, bootstrap)
    await screen.findByText('Preparing the secure sign-in service')

    act(() => emitBootstrap?.({ type: 'complete', completedAt: 2_000 }))

    expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
    expect(status).toHaveBeenCalledTimes(2)

    act(() => emitBootstrap?.({ type: 'complete', completedAt: 2_000 }))
    await act(async () => Promise.resolve())
    expect(status).toHaveBeenCalledTimes(2)
  })

  it('enters the protected root once after full runtime completion without an automatic refresh loop', async () => {
    let emitBootstrap: ((event: Record<string, unknown>) => void) | null = null
    const runtimePending = { ...authenticated, runtime_ready: false }
    const status = vi.fn().mockResolvedValueOnce(runtimePending).mockResolvedValueOnce(authenticated)
    const onMount = vi.fn()

    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          bootstrapScope: 'runtime',
          stages: [{ name: 'complete', title: 'Finish install' }]
        },
        stages: {
          complete: {
            state: 'running',
            durationMs: null,
            startedAt: 1_000,
            error: null,
            progress: null
          }
        },
        error: null,
        failedStage: null,
        startedAt: 500,
        completedAt: null
      })),
      onChanged: vi.fn(callback => {
        emitBootstrap = callback

        return () => {
          emitBootstrap = null
        }
      }),
      retry: vi.fn(async () => ({ ok: true }))
    }

    const { auth } = renderGate({ status }, null, <ProtectedMountProbe onMount={onMount} />, bootstrap)

    await screen.findByText('Finish install')
    act(() => emitBootstrap?.({ type: 'complete', completedAt: 2_000 }))

    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()
    expect(onMount).toHaveBeenCalledTimes(1)
    expect(auth.status).toHaveBeenCalledTimes(2)

    act(() => emitBootstrap?.({ type: 'complete', completedAt: 2_000 }))
    await act(async () => Promise.resolve())
    expect(onMount).toHaveBeenCalledTimes(1)
    expect(auth.status).toHaveBeenCalledTimes(2)
  })

  it('does not render hostile live progress text in the auth surface', async () => {
    let emitBootstrap: ((event: Record<string, unknown>) => void) | null = null

    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: {
          type: 'manifest',
          protocolVersion: 1,
          bootstrapScope: 'auth',
          stages: [{ name: 'python-auth-deps', title: 'Install authentication dependencies' }]
        },
        stages: {
          'python-auth-deps': {
            state: 'running',
            durationMs: null,
            startedAt: 1_000,
            error: null,
            progress: null
          }
        },
        error: null,
        failedStage: null,
        startedAt: 500,
        completedAt: null
      })),
      onChanged: vi.fn(callback => {
        emitBootstrap = callback

        return () => {
          emitBootstrap = null
        }
      }),
      retry: vi.fn(async () => ({ ok: true }))
    }

    renderGate(
      { status: vi.fn(() => new Promise<DesktopAccountStatus>(() => {})) },
      null,
      <div>Protected Hermes application</div>,
      bootstrap
    )
    await screen.findByText('Preparing the secure sign-in service')

    act(() =>
      emitBootstrap?.({
        type: 'progress',
        stage: 'python-auth-deps',
        completed: 1,
        total: null,
        unit: 'packages',
        label: 'password=secret Cookie: abc /Users/alice/private',
        updatedAt: 2_000
      })
    )

    expect(globalThis.document.body.textContent).not.toContain('secret')
    expect(globalThis.document.body.textContent).not.toContain('Cookie')
    expect(globalThis.document.body.textContent).not.toContain('/Users/')
  })

  it('turns a bootstrap failure into a safe terminal state and retries through the bridge', async () => {
    let emitBootstrap: ((event: Record<string, unknown>) => void) | null = null
    const retryBootstrap = vi.fn(async () => ({ ok: true }))

    const bootstrap = {
      getState: vi.fn(async () => ({
        active: true,
        manifest: { type: 'manifest', protocolVersion: 1, stages: [] },
        stages: {},
        error: null,
        log: [],
        startedAt: Date.now(),
        completedAt: null,
        setupChoice: null,
        unsupportedPlatform: null
      })),
      onChanged: vi.fn(callback => {
        emitBootstrap = callback

        return () => {
          emitBootstrap = null
        }
      }),
      retry: retryBootstrap
    }

    const status = vi.fn(() => new Promise<DesktopAccountStatus>(() => {}))

    renderGate({ status }, null, <div>Protected Hermes application</div>, bootstrap)

    await act(async () => {
      emitBootstrap?.({ type: 'failed', error: 'sessionid=secret Traceback private detail' })
    })

    expect(screen.getByText('Ansatz could not prepare the secure sign-in service.')).not.toBeNull()
    expect(globalThis.document.body.textContent).not.toContain('sessionid')
    expect(globalThis.document.body.textContent).not.toContain('Traceback')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(retryBootstrap).toHaveBeenCalledTimes(1))
    expect(status).toHaveBeenCalledTimes(2)
  })

  it('keeps signed bootstrap hidden until authentication completes', async () => {
    const { emit } = renderGate({}, <div>Signed auth runtime bootstrap</div>)

    expect(await screen.findByRole('heading', { name: 'Sign in to Ansatz' })).not.toBeNull()
    expect(screen.queryByText('Signed auth runtime bootstrap')).toBeNull()
    expect(screen.queryByText('Protected Hermes application')).toBeNull()

    act(() => emit(authenticated))

    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()
    expect(screen.queryByText('Signed auth runtime bootstrap')).toBeNull()
  })

  it('maps server failures to safe reason text without rendering raw errors', async () => {
    renderGate({
      status: vi.fn(async () => ({ ...signedOut, state: 'locked', reason: 'server_unavailable' }))
    })

    expect(await screen.findByText('The account server is unavailable. Try again.')).not.toBeNull()
    expect(globalThis.document.body.textContent).not.toContain('Traceback')
    expect(globalThis.document.body.textContent).not.toContain('sessionid')
  })

  it('routes a remote connection lock and login through that exact connection id', async () => {
    const { auth, emit } = renderGate({
      status: vi.fn(async connectionId => (connectionId === 'remote-a' ? signedOut : authenticated))
    })

    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()

    act(() => emit({ ...signedOut, runtime_instance_id: 'remote-runtime' }, 'remote-a'))
    fireEvent.change(await screen.findByLabelText('Username'), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password-sentinel' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(auth.login).toHaveBeenCalledWith('alice', 'password-sentinel', 'remote-a'))
  })
})
