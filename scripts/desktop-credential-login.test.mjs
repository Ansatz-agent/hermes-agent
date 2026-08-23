import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const driverPath = path.join(import.meta.dirname, 'desktop-credential-login.mjs')

test('credential frame requires exactly two non-empty NUL-delimited values', async () => {
  assert.ok(fs.existsSync(driverPath), 'credential login driver must exist')
  const { parseCredentialFrame } = await import('./desktop-credential-login.mjs')

  assert.deepEqual(parseCredentialFrame(Buffer.from('test-user\0password-sentinel\0')), {
    username: 'test-user',
    password: 'password-sentinel'
  })
  assert.throws(() => parseCredentialFrame(Buffer.from('test-user\0\0')), /credential_frame_invalid/)
  assert.throws(() => parseCredentialFrame(Buffer.from('test-user\0password-sentinel')), /credential_frame_invalid/)
  assert.throws(
    () => parseCredentialFrame(Buffer.from('test-user\0password-sentinel\0extra')),
    /credential_frame_invalid/
  )
})

test('credential driver proves runtime progress without credential-bearing output paths', () => {
  assert.ok(fs.existsSync(driverPath), 'credential login driver must exist')
  const source = fs.readFileSync(driverPath, 'utf8')

  assert.match(source, /Account verified, preparing Hermes/)
  assert.match(source, /Hermes runtime installation/)
  assert.match(source, /stages complete/)
  assert.match(source, /Elapsed/)
  assert.doesNotMatch(source, /console\.(?:log|error).*username|console\.(?:log|error).*password/i)
  assert.doesNotMatch(source, /writeFile|appendFile|screenshot|process\.argv.*credential/i)
  assert.match(source, /127\.0\.0\.1/)
})
