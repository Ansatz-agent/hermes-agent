import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createSafeStorageTraceKeyProtector, decryptTraceRecord, encryptTraceRecord } from './trace-outbox-crypto'

test('wraps data keys only through the injected OS key protector', () => {
  let encryptionAvailable = true

  const safeStorage = {
    isEncryptionAvailable: () => encryptionAvailable,
    encryptString: (plaintext: string) => Buffer.from(Buffer.from(plaintext, 'utf8').map(byte => byte ^ 0xa5)),
    decryptString: (wrapped: Buffer) => Buffer.from(wrapped.map(byte => byte ^ 0xa5)).toString('utf8')
  }

  const protector = createSafeStorageTraceKeyProtector(safeStorage)
  const key = Buffer.alloc(32, 23)
  const wrapped = protector.wrap(key)

  assert.equal(protector.available(), true)
  assert.notDeepEqual(wrapped, key)
  assert.deepEqual(protector.unwrap(wrapped), key)

  encryptionAvailable = false
  assert.equal(protector.available(), false)
  assert.throws(() => protector.wrap(key), /secure_key_storage_unavailable/)
})

test('compresses before encryption and authenticates account metadata', async () => {
  const body = Buffer.from('repeated history '.repeat(10_000))
  const key = Buffer.alloc(32, 7)
  const aad = Buffer.from('account-a/batch-1')

  const encrypted = await encryptTraceRecord(body, key, aad)

  assert.ok(encrypted.ciphertext.length < body.length)
  assert.equal(encrypted.nonce.length, 12)
  assert.equal(encrypted.tag.length, 16)
  assert.equal(encrypted.originalBytes, body.length)
  assert.deepEqual(await decryptTraceRecord(encrypted, key, aad), body)
  await assert.rejects(decryptTraceRecord(encrypted, key, Buffer.from('account-b/batch-1')))
})

test('uses a fresh nonce for every independently encrypted record', async () => {
  const body = Buffer.from('same trace batch')
  const key = Buffer.alloc(32, 11)
  const aad = Buffer.from('account-a/batch-1')

  const first = await encryptTraceRecord(body, key, aad)
  const second = await encryptTraceRecord(body, key, aad)

  assert.notDeepEqual(first.nonce, second.nonce)
  assert.notDeepEqual(first.ciphertext, second.ciphertext)
})

test('rejects a wrong key, tampered ciphertext, and malformed AES-GCM inputs', async () => {
  const body = Buffer.from('private trace payload')
  const key = Buffer.alloc(32, 19)
  const aad = Buffer.from('account-a/batch-2')
  const encrypted = await encryptTraceRecord(body, key, aad)

  const tampered = {
    ...encrypted,
    ciphertext: Buffer.from(encrypted.ciphertext)
  }

  tampered.ciphertext[0] ^= 1

  await assert.rejects(decryptTraceRecord(encrypted, Buffer.alloc(32, 20), aad))
  await assert.rejects(decryptTraceRecord(tampered, key, aad))
  await assert.rejects(decryptTraceRecord({ ...encrypted, nonce: Buffer.alloc(11) }, key, aad), /invalid_nonce/)
  await assert.rejects(decryptTraceRecord({ ...encrypted, tag: Buffer.alloc(15) }, key, aad), /invalid_auth_tag/)
  await assert.rejects(encryptTraceRecord(body, Buffer.alloc(31), aad), /invalid_encryption_key/)
})
