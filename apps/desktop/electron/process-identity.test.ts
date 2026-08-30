import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  backendExitedBeforeOwnershipError,
  isProcessGoneError,
  WINDOWS_PROCESS_NOT_FOUND_EXIT_CODE,
  windowsProcessStartMarkerCommand
} from './process-identity'

test('Windows process marker command treats a vanished PID as a classified exit', () => {
  const command = windowsProcessStartMarkerCommand(18812)

  assert.match(command, /Get-Process -Id 18812 -ErrorAction SilentlyContinue/)
  assert.match(command, new RegExp(`exit ${WINDOWS_PROCESS_NOT_FOUND_EXIT_CODE}`))
  assert.doesNotMatch(command, /ErrorAction Stop/)
})

test('process-gone detection handles Windows exit codes and localized diagnostics', () => {
  assert.equal(isProcessGoneError({ code: WINDOWS_PROCESS_NOT_FOUND_EXIT_CODE }), true)
  assert.equal(isProcessGoneError({ code: 'ESRCH' }), true)
  assert.equal(isProcessGoneError({ message: 'Get-Process: NoProcessFoundForGivenId' }), true)
  assert.equal(isProcessGoneError({ message: 'permission denied' }), false)
})

test('backend exit is reported separately from ownership persistence failure', () => {
  const cause = new Error('process disappeared')
  const error = backendExitedBeforeOwnershipError(18812, cause)

  assert.equal(error.code, 'BACKEND_EXITED')
  assert.match(error.message, /process 18812 exited before ownership could be recorded/i)
  assert.equal(error.cause, cause)
})
