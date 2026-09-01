import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  isExactWindowsAuthOwnerProcess,
  retireExactWindowsAuthOwners,
  type WindowsProcessRecord
} from './windows-auth-owner'

const ACTIVE_ROOT = String.raw`C:\Users\张 三\AppData\Local\hermes\hermes-agent`
const PYTHON = path.win32.join(ACTIVE_ROOT, 'auth-venv', 'python.exe')
const LEGACY_PYTHONW = path.win32.join(ACTIVE_ROOT, 'venv', 'Scripts', 'pythonw.exe')
const SID = 'S-1-5-21-100-200-300-400'

function ownerRecord(overrides: Partial<WindowsProcessRecord> = {}): WindowsProcessRecord {
  return {
    processId: 4242,
    executablePath: PYTHON,
    commandLine: `"${PYTHON}" -m hermes_cli.client_auth.runtime owner`,
    ownerSid: SID,
    ...overrides
  }
}

test('exact Windows auth owner requires SID, interpreter, and complete command identity', () => {
  assert.equal(
    isExactWindowsAuthOwnerProcess(ownerRecord(), {
      activeRoot: ACTIVE_ROOT,
      currentSid: SID,
      excludedPids: new Set([99])
    }),
    true
  )

  const rejected: Array<[string, Partial<WindowsProcessRecord>]> = [
    ['another SID', { ownerSid: 'S-1-5-21-other' }],
    ['another root', { executablePath: String.raw`C:\other\auth-venv\python.exe` }],
    ['bridge process', { commandLine: `"${PYTHON}" -m hermes_cli.client_auth.bridge` }],
    ['suffix spoof', { commandLine: `"${PYTHON}" -m hermes_cli.client_auth.runtime owner --extra` }],
    ['prefix spoof', { commandLine: `launcher "${PYTHON}" -m hermes_cli.client_auth.runtime owner` }],
    ['missing command', { commandLine: null }],
    ['missing executable', { executablePath: null }],
    ['missing SID', { ownerSid: null }],
    ['invalid PID', { processId: 0 }],
    ['caller PID', { processId: 99 }]
  ]

  for (const [label, overrides] of rejected) {
    assert.equal(
      isExactWindowsAuthOwnerProcess(ownerRecord(overrides), {
        activeRoot: ACTIVE_ROOT,
        currentSid: SID,
        excludedPids: new Set([99])
      }),
      false,
      label
    )
  }
})

test('exact Windows auth owner path comparison is canonical and case-insensitive', () => {
  const equivalentRoot = String.raw`c:\USERS\张 三\AppData\Local\hermes\nested\..\hermes-agent`
  const equivalentPython = String.raw`C:\users\张 三\appdata\local\HERMES\hermes-agent\auth-venv\PYTHON.EXE`

  assert.equal(
    isExactWindowsAuthOwnerProcess(
      ownerRecord({
        executablePath: equivalentPython,
        commandLine: `"${equivalentPython}" -m hermes_cli.client_auth.runtime owner`
      }),
      {
        activeRoot: equivalentRoot,
        currentSid: SID,
        excludedPids: new Set()
      }
    ),
    true
  )
})

test('legacy venv auth owners are accepted only when explicitly included', () => {
  const record = ownerRecord({
    executablePath: LEGACY_PYTHONW,
    commandLine: `"${LEGACY_PYTHONW}" -m hermes_cli.client_auth.runtime owner`
  })

  assert.equal(
    isExactWindowsAuthOwnerProcess(record, {
      activeRoot: ACTIVE_ROOT,
      currentSid: SID,
      excludedPids: new Set()
    }),
    false
  )
  assert.equal(
    isExactWindowsAuthOwnerProcess(record, {
      activeRoot: ACTIVE_ROOT,
      currentSid: SID,
      excludedPids: new Set(),
      expectedExecutables: [LEGACY_PYTHONW]
    }),
    true
  )
})

test('owner retirement passes install root as data and revalidates each selected PID', async () => {
  const calls: Array<{ args: string[]; env: NodeJS.ProcessEnv; timeout: number }> = []
  const inventory = JSON.stringify({ currentSid: SID, processes: [ownerRecord()] })

  const result = await retireExactWindowsAuthOwners({
    activeRoot: ACTIVE_ROOT,
    callerPids: [99],
    runPowerShell: async (_command, args, options) => {
      calls.push({ args, env: options.env, timeout: options.timeout })

      return calls.length === 1
        ? { status: 0, stdout: inventory, stderr: '' }
        : { status: 0, stdout: JSON.stringify({ stopped: true, processId: 4242 }), stderr: '' }
    }
  })

  assert.deepEqual(result, { inspected: 1, stopped: 1 })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].env.HERMES_AUTH_OWNER_ROOT, ACTIVE_ROOT)
  assert.equal(calls[1].env.HERMES_AUTH_OWNER_PID, '4242')
  assert.equal(calls[1].env.HERMES_AUTH_OWNER_SID, SID)
  assert.equal(calls[0].args.includes(ACTIVE_ROOT), false)
  assert.equal(calls[1].args.includes(ACTIVE_ROOT), false)
  assert.equal(
    calls.every(call => call.timeout > 0 && call.timeout <= 10_000),
    true
  )
  assert.equal(
    calls.every(call => {
      const encodedIndex = call.args.indexOf('-EncodedCommand')
      const script = Buffer.from(call.args[encodedIndex + 1], 'base64').toString('utf16le')

      return script.includes('[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)')
    }),
    true
  )
})

test('legacy owner retirement opts into the legacy interpreter paths', async () => {
  const calls: Array<{ env: NodeJS.ProcessEnv; script: string }> = []
  const record = ownerRecord({
    executablePath: LEGACY_PYTHONW,
    commandLine: `"${LEGACY_PYTHONW}" -m hermes_cli.client_auth.runtime owner`
  })

  const result = await retireExactWindowsAuthOwners({
    activeRoot: ACTIVE_ROOT,
    includeLegacyVenv: true,
    callerPids: [99],
    runPowerShell: async (_command, args, options) => {
      const encodedIndex = args.indexOf('-EncodedCommand')
      calls.push({
        env: options.env,
        script: Buffer.from(args[encodedIndex + 1], 'base64').toString('utf16le')
      })

      return calls.length === 1
        ? { status: 0, stdout: JSON.stringify({ currentSid: SID, processes: [record] }), stderr: '' }
        : { status: 0, stdout: JSON.stringify({ stopped: true, processId: record.processId }), stderr: '' }
    }
  })

  assert.deepEqual(result, { inspected: 1, stopped: 1 })
  assert.equal(calls[0].env.HERMES_AUTH_OWNER_INCLUDE_LEGACY_VENV, '1')
  assert.match(calls[0].script, /venv\\Scripts\\pythonw\.exe/)
})

test('owner retirement accepts proof that an inventoried PID is no longer the auth owner', async () => {
  let calls = 0

  const result = await retireExactWindowsAuthOwners({
    activeRoot: ACTIVE_ROOT,
    callerPids: [99],
    runPowerShell: async () => {
      calls += 1

      return calls === 1
        ? {
            status: 0,
            stdout: JSON.stringify({ currentSid: SID, processes: [ownerRecord()] }),
            stderr: ''
          }
        : {
            status: 0,
            stdout: JSON.stringify({ noLongerOwner: true, processId: 4242, stopped: false }),
            stderr: ''
          }
    }
  })

  assert.deepEqual(result, { inspected: 1, stopped: 0 })
  assert.equal(calls, 2)
})

test('owner retirement still fails closed when PID identity cannot be inspected', async () => {
  let calls = 0

  await assert.rejects(
    retireExactWindowsAuthOwners({
      activeRoot: ACTIVE_ROOT,
      callerPids: [99],
      runPowerShell: async () => {
        calls += 1

        return calls === 1
          ? {
              status: 0,
              stdout: JSON.stringify({ currentSid: SID, processes: [ownerRecord()] }),
              stderr: ''
            }
          : { status: 7, stdout: '', stderr: 'SID lookup failed' }
      }
    }),
    /could not be safely retired/i
  )
  assert.equal(calls, 2)
})

test('owner retirement fails closed when an exact interpreter candidate has incomplete identity', async () => {
  let calls = 0

  await assert.rejects(
    retireExactWindowsAuthOwners({
      activeRoot: ACTIVE_ROOT,
      callerPids: [99],
      runPowerShell: async () => {
        calls += 1

        return {
          status: 0,
          stdout: JSON.stringify({
            currentSid: SID,
            processes: [ownerRecord({ commandLine: null })]
          }),
          stderr: ''
        }
      }
    }),
    /inspection returned invalid data/i
  )
  assert.equal(calls, 1)
})

test('owner retirement ignores unrelated Python processes', async () => {
  let calls = 0

  const result = await retireExactWindowsAuthOwners({
    activeRoot: ACTIVE_ROOT,
    callerPids: [99],
    runPowerShell: async () => {
      calls += 1

      return {
        status: 0,
        stdout: JSON.stringify({
          currentSid: SID,
          processes: [
            ownerRecord({ commandLine: `"${PYTHON}" -m hermes_cli.client_auth.bridge` }),
            ownerRecord({ processId: 8888, executablePath: String.raw`C:\Python\python.exe` })
          ]
        }),
        stderr: ''
      }
    }
  })

  assert.deepEqual(result, { inspected: 2, stopped: 0 })
  assert.equal(calls, 1)
})

test('owner retirement yields while PowerShell inventory is pending', async () => {
  let resolveInventory!: (value: { status: number; stdout: string; stderr: string }) => void

  const inventory = new Promise<{ status: number; stdout: string; stderr: string }>(resolve => {
    resolveInventory = resolve
  })

  const retirement = retireExactWindowsAuthOwners({
    activeRoot: ACTIVE_ROOT,
    callerPids: [99],
    runPowerShell: () => inventory
  })

  let settled = false
  void Promise.resolve(retirement).finally(() => {
    settled = true
  })

  await Promise.resolve()
  assert.equal(settled, false)

  resolveInventory({
    status: 0,
    stdout: JSON.stringify({ currentSid: SID, processes: [] }),
    stderr: ''
  })
  assert.deepEqual(await retirement, { inspected: 0, stopped: 0 })
})

test('owner retirement enforces one fail-closed aggregate deadline', async () => {
  let calls = 0
  let nowMs = 0

  const records = [ownerRecord({ processId: 4001 }), ownerRecord({ processId: 4002 }), ownerRecord({ processId: 4003 })]

  await assert.rejects(
    retireExactWindowsAuthOwners({
      activeRoot: ACTIVE_ROOT,
      callerPids: [99],
      now: () => nowMs,
      runPowerShell: async () => {
        calls += 1
        nowMs += 10_000

        return calls === 1
          ? {
              status: 0,
              stdout: JSON.stringify({ currentSid: SID, processes: records }),
              stderr: ''
            }
          : {
              status: 0,
              stdout: JSON.stringify({ stopped: true, processId: records[calls - 2].processId }),
              stderr: ''
            }
      }
    }),
    /aggregate deadline/i
  )
  assert.equal(calls, 3)
})
