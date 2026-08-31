import assert from 'node:assert/strict'

import { test } from 'vitest'

import { exeIdentityOptions } from './exe-identity-options.mjs'

test('Ansatz executable identity uses the product version for both PE version fields', () => {
  const options = exeIdentityOptions({
    icon: 'C:\\fixture\\ansatz.ico',
    productVersion: '0.17.0'
  })

  assert.equal(options['file-version'], '0.17.0')
  assert.equal(options['product-version'], '0.17.0')
  assert.equal(options['version-string'].ProductName, 'Ansatz')
  assert.equal(options.icon, 'C:\\fixture\\ansatz.ico')
})

test('Ansatz executable identity rejects a missing product version', () => {
  assert.throws(
    () => exeIdentityOptions({ icon: 'ansatz.ico', productVersion: '' }),
    /product version is required/
  )
})
