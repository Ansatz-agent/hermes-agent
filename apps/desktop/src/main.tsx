import './styles.css'
// Dev-only render/state churn counters. MUST precede the `react-dom` import
// below: react-dom captures the devtools hook at module init, so bippy has to
// install during THIS import's evaluation or every commit goes unseen
// (verified — a late install reports renderers=0, commits=0). `vite.config.ts`
// aliases this specifier to a no-op module for non-dev builds, so neither the
// counters nor bippy reach a shipped renderer.
import '@/debug/dev-only'

import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'

import { AuthGate } from './components/auth-gate'
import { RootErrorBoundary } from './components/error-boundary'
import { I18nProvider } from './i18n'

const ProtectedRoot = lazy(() => import('./protected-root'))

const winParam = new URLSearchParams(window.location.search).get('win')

if (winParam === 'hud') {
  document.title = 'Ansatz HUD'
}

if (winParam === 'overlay') {
  void import('./app/pet-overlay/overlay-root').then(({ mountPetOverlay }) => mountPetOverlay())
} else if (winParam === 'quick') {
  void import('./app/quick-entry/quick-entry-root').then(({ mountQuickEntry }) => mountQuickEntry())
} else if (winParam === 'wake') {
  void import('./app/wake-indicator/wake-indicator-root').then(({ mountWakeIndicator }) => mountWakeIndicator())
} else {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <RootErrorBoundary>
        <I18nProvider configClient={null} initialLocale={navigator.language}>
          <AuthGate>
            <Suspense fallback={null}>
              <ProtectedRoot />
            </Suspense>
          </AuthGate>
        </I18nProvider>
      </RootErrorBoundary>
    </StrictMode>
  )
}
