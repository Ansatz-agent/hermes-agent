import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { I18nProvider } from '@/i18n'
import { $gateway } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'

import { ChatRoutesSurface, StatusbarSurface } from './surfaces'
import type { WiringActions } from './types'

const desktopAuth = vi.hoisted(() => ({
  logout: vi.fn(),
  status: {
    epoch: 2,
    reason: null,
    runtime_instance_id: 'runtime-1',
    session_expires_at: null,
    state: 'authenticated' as const,
    username: 'alice',
    valid_until: 60
  }
}))

vi.mock('@/contrib/react/use-contributions', () => ({ useContributions: vi.fn() }))
vi.mock('@/components/auth-gate', () => ({
  useDesktopAuth: () => ({ connectionId: 'local', logout: desktopAuth.logout, status: desktopAuth.status })
}))
vi.mock('@/store/gateway', () => ({ $gateway: atom<unknown>(null) }))
vi.mock('@/store/profile', () => ({ $activeGatewayProfile: atom('default') }))
vi.mock('@/store/session', () => ({
  $freshDraftReady: atom(false),
  $gatewayState: atom('open')
}))
vi.mock('../chat', () => ({
  ChatView: ({ gateway }: { gateway: { id?: string } | null }) => <div data-testid="gateway">{gateway?.id}</div>
}))
vi.mock('../chat/sidebar', () => ({ ChatSidebar: () => null }))
vi.mock('../right-sidebar/terminal/chrome', () => ({ TerminalPaneChrome: () => null }))
vi.mock('../shell/hooks/use-status-snapshot', () => ({ useStatusSnapshot: () => ({}) }))
vi.mock('../shell/hooks/use-statusbar-items', () => ({
  useStatusbarItems: () => ({ leftStatusbarItems: [], statusbarItems: [] })
}))
vi.mock('../routes', () => ({
  contributedRoutes: () => [],
  NEW_CHAT_ROUTE: '/new',
  ROUTES_AREA: 'routes',
  sessionRoute: (id: string) => `/${id}`
}))
vi.mock('./latest-actions', () => ({ latestChatActions: () => ({}), latestSidebarActions: () => ({}) }))
vi.mock('./panes', () => ({ setStatusbarItemGroup: vi.fn(), useStatusbarContributions: () => [] }))
vi.mock('../shell/model-menu-panel', () => ({ ModelMenuPanel: () => null }))

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

afterEach(() => {
  cleanup()
  $gateway.set(null)
  $activeGatewayProfile.set('default')
  desktopAuth.logout.mockReset()
})

const statusbarActions = {
  openAgents: vi.fn(),
  openCommandCenterSection: vi.fn(),
  requestGateway: vi.fn(async () => ({})),
  toggleCommandCenter: vi.fn()
} as unknown as WiringActions

function renderStatusbar(locale: 'en' | 'zh' = 'en') {
  render(
    <I18nProvider configClient={null} initialLocale={locale}>
      <MemoryRouter>
        <StatusbarSurface actions={statusbarActions} agentsOpen={false} chatOpen commandCenterOpen={false} />
      </MemoryRouter>
    </I18nProvider>
  )
}

describe('ChatRoutesSurface', () => {
  it('passes the live gateway after an open-to-open profile switch', () => {
    const gatewayA = { id: 'a' } as unknown as HermesGateway
    const gatewayB = { id: 'b' } as unknown as HermesGateway

    $gateway.set(gatewayA)
    const actions = { getGateway: () => $gateway.get() } as unknown as WiringActions

    render(
      <MemoryRouter>
        <ChatRoutesSurface actions={actions} />
      </MemoryRouter>
    )

    expect(screen.getByTestId('gateway').textContent).toBe('a')

    act(() => {
      $gateway.set(gatewayB)
      $activeGatewayProfile.set('other')
    })

    expect(screen.getByTestId('gateway').textContent).toBe('b')
  })
})

describe('StatusbarSurface account menu', () => {
  it('shows the authenticated username and signs out through the shared menu', async () => {
    desktopAuth.logout.mockResolvedValue({ ...desktopAuth.status, state: 'signed_out', username: null })
    renderStatusbar()

    const trigger = await screen.findByRole('button', { name: /alice/i })
    fireEvent.pointerDown(trigger, { button: 0 })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Sign out' }))

    await waitFor(() => expect(desktopAuth.logout).toHaveBeenCalledTimes(1))
  })

  it('renders the sign-out action in Chinese', async () => {
    renderStatusbar('zh')

    fireEvent.pointerDown(await screen.findByRole('button', { name: /alice/i }), { button: 0 })

    expect(await screen.findByRole('menuitem', { name: '退出登录' })).not.toBeNull()
  })

  it('suppresses another sign-out while the first request is pending', async () => {
    desktopAuth.logout.mockReturnValue(new Promise(() => undefined))
    renderStatusbar()

    const trigger = await screen.findByRole('button', { name: /alice/i })
    fireEvent.pointerDown(trigger, { button: 0 })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Sign out' }))
    fireEvent.pointerDown(trigger, { button: 0 })

    expect(desktopAuth.logout).toHaveBeenCalledTimes(1)
    expect(trigger.hasAttribute('disabled')).toBe(true)
  })
})
