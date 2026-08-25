import { describe, expect, it } from 'vitest'

import { ar } from './ar'
import { en } from './en'
import { ja } from './ja'
import { zh } from './zh'
import { zhHant } from './zh-hant'

import { validationHealthText } from './index'

const requiredReasonKeys = [
  'interactiveLoginRequired',
  'invalidCredentials',
  'rateLimited',
  'runtimeUnavailable',
  'serverUnavailable',
  'sessionExpired',
  'sessionRejected',
  'signedOut',
  'vaultUnavailable'
] as const

const requiredAuthKeys = [
  'title',
  'description',
  'serverLabel',
  'username',
  'password',
  'signIn',
  'signingIn',
  'retry',
  'checking',
  'administratorManaged'
] as const

describe('account auth locale catalog', () => {
  it('has every hard-gate message in the required four catalogs', () => {
    for (const locale of [en, ja, zh, zhHant]) {
      for (const key of requiredAuthKeys) {
        expect(locale.auth[key].trim().length).toBeGreaterThan(0)
      }

      for (const key of requiredReasonKeys) {
        expect(locale.auth.reasons[key].trim().length).toBeGreaterThan(0)
      }
    }
  })

  it('uses the intentional English fallback for every Arabic auth message', () => {
    for (const key of requiredAuthKeys) {
      expect(ar.auth[key]).toBe(en.auth[key])
    }

    for (const key of requiredReasonKeys) {
      expect(ar.auth.reasons[key]).toBe(en.auth.reasons[key])
    }
  })

  it('offers no self-registration or self-service account recovery copy', () => {
    const copy = [en, ja, zh, zhHant]
      .flatMap(locale => [
        ...requiredAuthKeys.map(key => locale.auth[key]),
        ...requiredReasonKeys.map(key => locale.auth.reasons[key])
      ])
      .join('\n')

    expect(copy).not.toMatch(/\b(register|sign up|create account|forgot password|reset password|change password)\b/i)
    expect(en.auth.administratorManaged).toMatch(/server administrator/i)
  })

  it('uses a localized, generic validation-health message without exposing validation details', () => {
    for (const locale of [en, ja, zh, zhHant, ar]) {
      expect(validationHealthText(locale.auth)).toBe(locale.auth.reasons.serverUnavailable)
    }
  })
})
