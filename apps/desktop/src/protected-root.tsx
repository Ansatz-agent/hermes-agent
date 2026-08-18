// These modules start renderer-side capability reporting and therefore belong
// behind AuthGate. Importing this file is itself the authenticated transition.
import './store/active-work'
import './store/power'
import './store/translucency'

import { QueryClientProvider } from '@tanstack/react-query'
import { useEffect } from 'react'
import { HashRouter } from 'react-router'

import App from './app'
import { HapticsProvider } from './components/haptics-provider'
import { RootTooltipProvider } from './components/ui/tooltip'
import { I18nProvider } from './i18n'
import { installClipboardShim } from './lib/clipboard'
import { queryClient } from './lib/query-client'
import { installRendererAnimationPauseState } from './lib/renderer-loop-pause'
import { wipeSessionListsForGatewaySwitch } from './store/gateway-switch'
import { ThemeProvider } from './themes/context'

installClipboardShim()
installRendererAnimationPauseState()

if (import.meta.env.MODE !== 'production' || import.meta.env.VITE_PERF_PROBE === '1') {
  void import('./app/chat/perf-probe')
}

export default function ProtectedRoot() {
  useEffect(
    () => () => {
      wipeSessionListsForGatewaySwitch()
      queryClient.clear()
    },
    []
  )

  return (
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <ThemeProvider>
          <HapticsProvider>
            <RootTooltipProvider>
              <HashRouter useTransitions={false}>
                <App />
              </HashRouter>
            </RootTooltipProvider>
          </HapticsProvider>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>
  )
}
