import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

export function electronBinaryCandidates(platform: NodeJS.Platform, desktopRoot: string, repoRoot: string): string[] {
  const relative =
    platform === 'darwin'
      ? path.join('node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron')
      : path.join('node_modules', 'electron', 'dist', platform === 'win32' ? 'electron.exe' : 'electron')

  return [path.join(desktopRoot, relative), path.join(repoRoot, relative)]
}

type ResolveElectronBinaryOptions = {
  desktopRoot: string
  exists?: (candidate: string) => boolean
  lookup?: (command: 'where' | 'which') => string | null
  platform: NodeJS.Platform
  repoRoot: string
}

function lookupElectron(command: 'where' | 'which'): string | null {
  const result = spawnSync(command, ['electron'], { encoding: 'utf8' })

  return result.status === 0 && result.stdout.trim() ? result.stdout.trim().split(/\r?\n/, 1)[0] : null
}

export function resolveElectronBinary(options: ResolveElectronBinaryOptions): string {
  const exists = options.exists ?? fs.existsSync

  for (const candidate of electronBinaryCandidates(options.platform, options.desktopRoot, options.repoRoot)) {
    if (exists(candidate)) {
      return candidate
    }
  }

  const fromPath = (options.lookup ?? lookupElectron)(options.platform === 'win32' ? 'where' : 'which')
  if (fromPath) {
    return fromPath
  }

  throw new Error('Electron binary not found. Run "npm install" from the repo root to install devDependencies.')
}
