import fs from 'node:fs'
import path from 'node:path'

import { forbiddenBrowserDownloadLine } from './desktop-dmg-contract.mjs'

const PE_X64_MACHINE = 0x8664

export { forbiddenBrowserDownloadLine }

export function readPeMachine(filePath) {
  const buffer = fs.readFileSync(filePath)
  if (buffer.length < 136 || buffer.toString('ascii', 0, 2) !== 'MZ') {
    throw new Error(`${filePath} is not a valid PE executable`)
  }

  const peOffset = buffer.readUInt32LE(0x3c)
  if (peOffset + 6 > buffer.length || buffer.toString('binary', peOffset, peOffset + 4) !== 'PE\0\0') {
    throw new Error(`${filePath} is not a valid PE executable`)
  }

  return buffer.readUInt16LE(peOffset + 4)
}

export function assertX64HermesExecutable(releaseDir) {
  const exe = path.resolve(releaseDir, 'win-unpacked', 'Ansatz.exe')
  if (!fs.existsSync(exe) || readPeMachine(exe) !== PE_X64_MACHINE) {
    throw new Error(`packaged Ansatz executable must be x64: ${exe}`)
  }
  return exe
}

export function findNewestWindowsNsis(releaseDir, notBeforeMs = 0) {
  const absoluteReleaseDir = path.resolve(releaseDir)
  const candidates = fs.existsSync(absoluteReleaseDir)
    ? fs
        .readdirSync(absoluteReleaseDir)
        .filter(name => /^Ansatz-.+-win-x64\.exe$/i.test(name))
        .map(name => path.join(absoluteReleaseDir, name))
        .filter(filePath => fs.statSync(filePath).isFile())
        .filter(filePath => fs.statSync(filePath).mtimeMs >= notBeforeMs)
        .filter(filePath => {
          try {
            readPeMachine(filePath)
            return true
          } catch {
            return false
          }
        })
        .sort((left, right) => {
          const delta = fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs
          return delta || right.localeCompare(left)
        })
    : []

  if (candidates.length === 0) {
    throw new Error(`no current Windows x64 Ansatz NSIS installer found in ${absoluteReleaseDir}`)
  }
  return candidates[0]
}
