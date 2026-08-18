import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { AuthGate, type DesktopAccountStatus } from './auth-gate'

const signedOut: DesktopAccountStatus = {
  state: 'signed_out',
  username: null,
  runtime_instance_id: 'runtime-1',
  epoch: 1,
  valid_until: 0,
  session_expires_at: null,
  reason: 'signed_out'
}

const authenticated: DesktopAccountStatus = {
  ...signedOut,
  state: 'authenticated',
  username: 'alice',
  epoch: 2,
  valid_until: 60,
  reason: null
}

function renderGate(overrides: Record<string, unknown> = {}, unauthenticatedOverlay: ReactNode = null) {
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

  render(
    <I18nProvider configClient={null} initialLocale="en">
      <AuthGate auth={auth as any} unauthenticatedOverlay={unauthenticatedOverlay}>
        <div>Protected Hermes application</div>
      </AuthGate>
    </I18nProvider>
  )

  return { auth, emit: (status: DesktopAccountStatus, connectionId?: string) => changed?.(status, connectionId) }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AuthGate', () => {
  it('shows only the fixed account login surface while signed out', async () => {
    renderGate()

    expect(await screen.findByRole('heading', { name: 'Sign in to Hermes' })).not.toBeNull()
    expect(screen.getByText('https://c2sml.cn/agent')).not.toBeNull()
    expect(screen.getByLabelText('Username')).not.toBeNull()
    expect(screen.getByLabelText('Password').getAttribute('type')).toBe('password')
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Retry' })).not.toBeNull()
    expect(screen.getAllByText(/server administrator/i).length).toBeGreaterThan(0)
    expect(screen.queryByText('Protected Hermes application')).toBeNull()

    for (const forbidden of ['Register', 'Create account', 'Reset password', 'Change password', 'Skip', 'Offline']) {
      expect(screen.queryByText(forbidden, { exact: false })).toBeNull()
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

  it('unmounts the protected tree immediately when main emits a lock', async () => {
    const { emit } = renderGate({ status: vi.fn(async () => authenticated) })
    expect(await screen.findByText('Protected Hermes application')).not.toBeNull()

    act(() => emit({ ...signedOut, state: 'locked', reason: 'session_expired', epoch: 3 }))

    expect(screen.queryByText('Protected Hermes application')).toBeNull()
    expect(await screen.findByRole('heading', { name: 'Sign in to Hermes' })).not.toBeNull()
  })

  it('does not let an older status response overwrite a newer lock event', async () => {
    let resolveStatus: ((status: DesktopAccountStatus) => void) | null = null

    const pendingStatus = new Promise<DesktopAccountStatus>(resolve => {
      resolveStatus = resolve
    })

    const { emit } = renderGate({ status: vi.fn(() => pendingStatus) })

    act(() => emit({ ...signedOut, state: 'locked', reason: 'session_expired', epoch: 3 }))
    expect(await screen.findByRole('heading', { name: 'Sign in to Hermes' })).not.toBeNull()

    await act(async () => {
      resolveStatus?.(authenticated)
      await pendingStatus
    })

    expect(screen.queryByText('Protected Hermes application')).toBeNull()
    expect(screen.getByText('Your session expired. Sign in again.')).not.toBeNull()
  })

  it('shows signed bootstrap only before authentication', async () => {
    const { emit } = renderGate({}, <div>Signed auth runtime bootstrap</div>)

    expect(await screen.findByText('Signed auth runtime bootstrap')).not.toBeNull()
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
