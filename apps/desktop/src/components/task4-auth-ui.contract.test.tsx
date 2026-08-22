import { act, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

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

const authenticated = {
  ...signedOut,
  state: 'authenticated' as const,
  username: 'alice',
  epoch: 2,
  valid_until: 60,
  reason: null,
  runtime_ready: false
}

function renderGate({
  status = vi.fn(async () => signedOut),
  overlay = <div>Protected bootstrap overlay</div>
}: {
  status?: () => Promise<DesktopAccountStatus>
  overlay?: ReactNode
} = {}) {
  let changed: ((next: DesktopAccountStatus) => void) | null = null
  const auth = {
    status,
    login: vi.fn(async () => authenticated),
    logout: vi.fn(async () => signedOut),
    onChanged: vi.fn(callback => {
      changed = callback
      return () => {
        changed = null
      }
    })
  }

  render(
    <I18nProvider configClient={null} initialLocale="en">
      <AuthGate auth={auth as any} unauthenticatedOverlay={overlay}>
        <div>Protected Hermes application</div>
      </AuthGate>
    </I18nProvider>
  )

  return { emit: (next: DesktopAccountStatus) => changed?.(next) }
}

describe('Task 4 final-DMG auth UI contract', () => {
  it('mounts the fixed login surface before any protected bootstrap overlay', async () => {
    renderGate()

    expect(await screen.findByRole('heading', { name: 'Sign in to Hermes' })).not.toBeNull()
    expect(screen.queryByText('Protected bootstrap overlay')).toBeNull()
    expect(screen.queryByText('Protected Hermes application')).toBeNull()
  })

  it('does not expose Retry while an account status request is still pending', async () => {
    renderGate({ status: vi.fn(() => new Promise<DesktopAccountStatus>(() => {})) })

    expect(await screen.findByRole('heading', { name: 'Sign in to Hermes' })).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('keeps authenticated users locked until the full runtime is ready', async () => {
    const { emit } = renderGate()

    await screen.findByRole('heading', { name: 'Sign in to Hermes' })
    act(() => emit(authenticated as DesktopAccountStatus))

    expect(screen.queryByText('Protected Hermes application')).toBeNull()
  })
})
