import path from 'node:path'

export interface GitBashOptions {
  isWindows: boolean
  env: Record<string, string | undefined>
  fileExists: (filePath: string) => boolean
  findOnPath?: (command: string) => string | null
  /**
   * Product-specific runtime root.  Packaged Ansatz installs use
   * `%LOCALAPPDATA%\\AnsatzVoiceTraceClient`, while the upstream Hermes CLI
   * historically used `%LOCALAPPDATA%\\hermes`.
   */
  hermesHome?: string
}

/**
 * Locate bash.exe on Windows.
 * Resolution order (first match wins):
 *   1. HERMES_GIT_BASH_PATH env var override
 *   2. PortableGit under the active Hermes/Ansatz runtime root (install.ps1)
 *   3. Legacy PortableGit under %LOCALAPPDATA%\hermes\git\
 *   4. Standard Git for Windows install locations
 *   5. %LOCALAPPDATA%\Programs\Git\ (user-scoped)
 *   6. bash on PATH
 */
export function findGitBash(opts: GitBashOptions): string | null {
  const { isWindows, env, fileExists, findOnPath } = opts

  if (!isWindows) {
    return findOnPath ? findOnPath('bash') : null
  }

  // Respect HERMES_GIT_BASH_PATH if set (mirrors tools/environments/local.py:_find_bash).
  const gitBashPath = env.HERMES_GIT_BASH_PATH

  if (gitBashPath && fileExists(gitBashPath)) {
    return gitBashPath
  }

  const localAppData = env.LOCALAPPDATA || ''
  const candidates: string[] = []

  // Candidate paths are Windows paths regardless of host platform (tests run
  // on POSIX CI hosts too), so join with win32 semantics explicitly.
  const joinWin = path.win32.join

  // The packaged Ansatz desktop passes its dedicated runtime root to the
  // installer (`-HermesHome`).  Do not rely on a child PowerShell process
  // updating this process' environment after installation; resolve the
  // absolute path from the root and inspect the filesystem directly.
  // An explicit root supplied by the caller (the packaged Ansatz root) wins
  // over an ambient HERMES_HOME inherited from an older Hermes installation.
  const managedRoots = [opts.hermesHome, env.HERMES_HOME]
  if (localAppData) {
    // Keep the upstream Hermes location as a compatibility fallback for users
    // who installed the standalone CLI or an older desktop build.
    managedRoots.push(joinWin(localAppData, 'hermes'))
  }

  const seen = new Set<string>()
  for (const managedRoot of managedRoots) {
    if (!managedRoot) {
      continue
    }

    const normalizedRoot = joinWin(managedRoot)
    const rootKey = normalizedRoot.toLowerCase()

    if (seen.has(rootKey)) {
      continue
    }

    seen.add(rootKey)
    candidates.push(joinWin(normalizedRoot, 'git', 'bin', 'bash.exe'))
    candidates.push(joinWin(normalizedRoot, 'git', 'usr', 'bin', 'bash.exe'))
  }

  candidates.push(joinWin(env['ProgramFiles'] || 'C:\\Program Files', 'Git', 'bin', 'bash.exe'))
  candidates.push(joinWin(env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Git', 'bin', 'bash.exe'))

  if (localAppData) {
    candidates.push(joinWin(localAppData, 'Programs', 'Git', 'bin', 'bash.exe'))
  }

  for (const candidate of candidates) {
    if (fileExists(candidate)) {
      return candidate
    }
  }

  if (findOnPath) {
    const onPath = findOnPath('bash')

    if (onPath) {
      return onPath
    }
  }

  return null
}
