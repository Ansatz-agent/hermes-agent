import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { type StatusbarItem } from '@/app/shell/statusbar-controls'
import { useDesktopAuth } from '@/components/auth-gate'
import { useI18n } from '@/i18n'
import { Loader2, LogOut, User } from '@/lib/icons'

export function useAccountStatusbarItem(): StatusbarItem {
  const { logout, status } = useDesktopAuth()
  const { t } = useI18n()
  const copy = t.auth
  const [signingOut, setSigningOut] = useState(false)
  const mounted = useRef(true)
  const signingOutRef = useRef(false)

  // eslint-disable-next-line no-restricted-syntax -- lifecycle mount flag, not a reactive atom mirror
  useEffect(() => {
    mounted.current = true

    return () => {
      mounted.current = false
    }
  }, [])

  const signOut = useCallback(() => {
    if (signingOutRef.current) {
      return
    }

    signingOutRef.current = true
    setSigningOut(true)

    void logout().catch(() => {
      signingOutRef.current = false

      if (mounted.current) {
        setSigningOut(false)
      }
    })
  }, [logout])

  return useMemo<StatusbarItem>(
    () => ({
      disabled: signingOut,
      icon: <User className="size-3" />,
      id: 'desktop-account',
      label: status.username || copy.accountMenu,
      lockedVisible: true,
      menuAlign: 'end',
      menuItems: [
        {
          disabled: signingOut,
          icon: signingOut ? <Loader2 className="animate-spin" /> : <LogOut />,
          id: 'desktop-account-sign-out',
          label: signingOut ? copy.signingOut : copy.signOut,
          onSelect: signOut
        }
      ],
      title: copy.accountMenu,
      toggleLabel: copy.accountMenu,
      variant: 'menu'
    }),
    [copy.accountMenu, copy.signOut, copy.signingOut, signOut, signingOut, status.username]
  )
}
