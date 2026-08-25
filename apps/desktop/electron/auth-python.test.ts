import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveNoConsoleAuthPython } from './auth-python'

test('Windows auth Python uses an existing GUI-subsystem sibling', () => {
  const checked: string[] = []
  const resolved = resolveNoConsoleAuthPython('C:\\Hermes\\venv\\Scripts\\python.exe', true, candidate => {
    checked.push(candidate)
    return candidate === 'C:\\Hermes\\venv\\Scripts\\pythonw.exe'
  })

  assert.equal(resolved, 'C:\\Hermes\\venv\\Scripts\\pythonw.exe')
  assert.deepEqual(checked, ['C:\\Hermes\\venv\\Scripts\\pythonw.exe'])
})

test('auth Python leaves non-Windows and already-GUI executables unchanged', () => {
  const unexpectedProbe = () => {
    throw new Error('file existence must not be probed')
  }

  assert.equal(
    resolveNoConsoleAuthPython('/opt/hermes/venv/bin/python', false, unexpectedProbe),
    '/opt/hermes/venv/bin/python'
  )
  assert.equal(
    resolveNoConsoleAuthPython('C:\\Hermes\\venv\\Scripts\\pythonw.exe', true, unexpectedProbe),
    'C:\\Hermes\\venv\\Scripts\\pythonw.exe'
  )
})

test('Windows auth Python falls back when pythonw is unavailable', () => {
  assert.equal(
    resolveNoConsoleAuthPython('C:\\Hermes\\venv\\Scripts\\python.exe', true, () => false),
    'C:\\Hermes\\venv\\Scripts\\python.exe'
  )
  assert.equal(
    resolveNoConsoleAuthPython('C:\\Python\\py.exe', true, () => true),
    'C:\\Python\\py.exe'
  )
})
