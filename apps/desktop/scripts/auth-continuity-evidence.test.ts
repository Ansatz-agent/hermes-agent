import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import * as authAssertions from '../e2e/auth-assertions'

const scratchRoots: string[] = []
const clientRoot = path.resolve(import.meta.dirname, '..', '..', '..')

function scratchRoot(): string {
  const parent = path.join(process.cwd(), 'tmp', 'task13-evidence')
  fs.mkdirSync(parent, { recursive: true })
  const root = fs.mkdtempSync(path.join(parent, 'case-'))
  scratchRoots.push(root)

  return root
}

function pythonExecutable(): string {
  return process.env.HERMES_PYTHON || '/Users/yuxiaoy/miniconda3/envs/dl/bin/python'
}

function createSqliteRow(databasePath: string): void {
  fs.mkdirSync(path.dirname(databasePath), { recursive: true })
  const result = spawnSync(
    pythonExecutable(),
    [
      '-c',
      'import sys; from pathlib import Path; from hermes_state import SessionDB; db=SessionDB(Path(sys.argv[1])); db.create_session("session-real", source="desktop"); db.append_message("session-real", role="user", content="persisted real row"); db.close()',
      databasePath
    ],
    { cwd: clientRoot, encoding: 'utf8' }
  )
  assert.equal(result.status, 0, result.stderr)
}

function evidence(root: string) {
  return authAssertions.localDataDigests({
    hermesHome: path.join(root, 'hermes-home'),
    userDataDir: path.join(root, 'desktop-data'),
    python: pythonExecutable()
  }) as unknown as {
    artifacts: Record<string, { sha256: string; size: number }>
    sqlite: Record<
      string,
      {
        logical_sha256: string
        physical: {
          main: { sha256: string; size: number }
          shm?: { sha256: string; size: number }
          wal?: { sha256: string; size: number }
        }
      }
    >
  }
}

afterEach(() => {
  for (const root of scratchRoots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('local evidence reports SQLite main/WAL/SHM separately without enumerating credentials', () => {
  const root = scratchRoot()
  const hermesHome = path.join(root, 'hermes-home')
  const databasePath = path.join(hermesHome, 'state.db')
  createSqliteRow(databasePath)
  fs.writeFileSync(`${databasePath}-wal`, Buffer.from('wal-evidence'))
  fs.writeFileSync(`${databasePath}-shm`, Buffer.from('shm-evidence'))
  fs.writeFileSync(path.join(root, 'auth-credential-store.json'), 'credential-secret')

  const observed = evidence(root)

  assert.equal(observed.sqlite['hermes/state.db'].physical.main.size > 0, true)
  assert.equal(observed.sqlite['hermes/state.db'].physical.wal?.size, 12)
  assert.equal(observed.sqlite['hermes/state.db'].physical.shm?.size, 12)
  assert.equal(JSON.stringify(observed).includes('auth-credential-store'), false)
  assert.equal(JSON.stringify(observed).includes('credential-secret'), false)
})

test('continuity comparison rejects a deleted sidecar and a deleted real SQLite row', () => {
  const root = scratchRoot()
  const hermesHome = path.join(root, 'hermes-home')
  const databasePath = path.join(hermesHome, 'state.db')
  createSqliteRow(databasePath)
  fs.writeFileSync(`${databasePath}-wal`, Buffer.from('wal-evidence'))
  fs.writeFileSync(`${databasePath}-shm`, Buffer.from('shm-evidence'))
  const before = evidence(root)
  const assertPreserved = (
    authAssertions as unknown as {
      assertLocalDataPreserved?: (left: typeof before, right: typeof before) => void
    }
  ).assertLocalDataPreserved
  assert.equal(typeof assertPreserved, 'function')

  fs.rmSync(`${databasePath}-wal`)
  const withoutWal = evidence(root)
  assert.throws(() => assertPreserved!(before, withoutWal), /SQLite sidecar was removed/)

  fs.writeFileSync(`${databasePath}-wal`, Buffer.from('wal-evidence'))
  const deleteResult = spawnSync(
    pythonExecutable(),
    [
      '-c',
      'import sys; from pathlib import Path; from hermes_state import SessionDB; db=SessionDB(Path(sys.argv[1])); assert db.delete_session("session-real"); db.close()',
      databasePath
    ],
    { cwd: clientRoot, encoding: 'utf8' }
  )
  assert.equal(deleteResult.status, 0, deleteResult.stderr)
  const withoutRow = evidence(root)
  assert.throws(() => assertPreserved!(before, withoutRow), /SQLite logical content changed/)
})
