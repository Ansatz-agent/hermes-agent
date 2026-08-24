import assert from 'node:assert/strict'
import fs from 'node:fs'

import { test } from 'vitest'

const bootstrapSource = fs.readFileSync(new URL('./bootstrap-runner.ts', import.meta.url), 'utf8')
const mainSource = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')

function extractFunction(source: string, signature: string): string {
  const start = source.indexOf(signature)
  assert.notEqual(start, -1, `${signature} should exist`)
  const end = source.indexOf('\nfunction ', start + signature.length)

  return source.slice(start, end === -1 ? undefined : end)
}

test('bundled runtime validation receives and applies the selected runtime home', () => {
  const validation = extractFunction(bootstrapSource, 'function validateBundledRuntime(')
  const runner = extractFunction(bootstrapSource, 'async function runBootstrap(')

  assert.match(validation, /function validateBundledRuntime\(activeRoot, hermesHome, bootstrapScope = 'runtime'\)/)
  assert.match(
    validation,
    /env: buildBundledRuntimeValidationEnvironment\(activeRoot, hermesHome\)/
  )
  assert.match(runner, /await validateBundledRuntime\(activeRoot, hermesHome, bootstrapScope\)/)
})

test('terminal shell delegates identity and runtime-home pinning to the tested environment policy', () => {
  const terminalEnvironment = extractFunction(mainSource, 'function terminalShellEnv(')

  assert.match(
    terminalEnvironment,
    /return buildAnsatzTerminalEnvironment\(HERMES_HOME, app\.getVersion\(\), sanitizeAuthChildEnvironment\(\)\)/
  )
})

test('both desktop SSH connection paths keep control sockets under the selected runtime home', () => {
  const authConnection = extractFunction(mainSource, 'async function prepareRegistryConnectionAuth(')
  const backendConnection = extractFunction(mainSource, 'async function bootstrapSshConnectionInner(')
  const expected = /controlDir: resolveAnsatzSshControlDirectory\(HERMES_HOME\)/

  assert.equal([...mainSource.matchAll(/new SshConnection\(/g)].length, 2)
  assert.match(authConnection, expected)
  assert.match(backendConnection, expected)
})
