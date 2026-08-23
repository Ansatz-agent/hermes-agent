import type { Page, Request } from '@playwright/test'

export async function protectedIpcRejections(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const desktop = (
      window as unknown as {
        hermesDesktop: {
          getConnection: () => Promise<unknown>
          terminal: { start: (options: { cwd: string }) => Promise<unknown> }
          signalDeepLinkReady: () => Promise<unknown>
          hud?: { open: () => Promise<unknown> }
          quickEntry: { getSettings: () => Promise<unknown> }
          petOverlay: {
            open: (options: { bounds: { height: number; width: number; x: number; y: number } }) => Promise<unknown>
          }
        }
      }
    ).hermesDesktop
    const attempt = async (operation: () => Promise<unknown>): Promise<string> => {
      try {
        await operation()
        return 'ALLOWED'
      } catch (error) {
        return String(error)
      }
    }

    return Promise.all([
      attempt(() => desktop.getConnection()),
      attempt(() => desktop.terminal.start({ cwd: '/' })),
      attempt(() => desktop.signalDeepLinkReady()),
      attempt(() => desktop.hud!.open()),
      attempt(() => desktop.quickEntry.getSettings()),
      attempt(() => desktop.petOverlay.open({ bounds: { height: 100, width: 100, x: 0, y: 0 } }))
    ])
  })
}

export async function authStatus(page: Page): Promise<{
  state: 'checking' | 'authenticated' | 'signed_out' | 'locked'
  username: string | null
}> {
  const status = await page.evaluate(() =>
    (
      window as unknown as {
        hermesDesktop: {
          auth: {
            status: () => Promise<{
              state: 'checking' | 'authenticated' | 'signed_out' | 'locked'
              username: string | null
            }>
          }
        }
      }
    ).hermesDesktop.auth.status()
  )
  return { state: status.state, username: status.username }
}

export function trackProtectedRendererChunk(page: Page): () => boolean {
  let requested = false

  const onRequest = (request: Request) => {
    if (/(?:^|\/)protected-root-[^/]+\.js(?:$|[?#])/.test(request.url())) {
      requested = true
    }
  }

  page.on('request', onRequest)

  return () => requested
}
