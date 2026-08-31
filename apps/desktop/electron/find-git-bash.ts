import path from 'node:path'

export interface GitBashOptions {
  isWindows: boolean
  env: Record<string, string | undefined>
  fileExists: (filePath: string) => boolean
  findOnPath?: (command: string) => string | null
}

/**
 * Locate bash.exe on Windows.
 * Resolution order (first match wins):
 *   1. HERMES_GIT_BASH_PATH env var override
 *   2. PortableGit under the active HERMES_HOME (install.ps1)
 *   3. PortableGit under the canonical Ansatz runtime root
 *   4. Legacy Hermes PortableGit root (for existing installs)
 *   5. Standard Git for Windows install locations
 *   6. %LOCALAPPDATA%\Programs\Git\ (user-scoped)
 *   7. bash on PATH
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

  const managedGitRoots = [
    env.HERMES_HOME ? joinWin(env.HERMES_HOME, 'git') : '',
    localAppData ? joinWin(localAppData, 'AnsatzVoiceTraceClient', 'git') : '',
    // Keep older Hermes-managed installs usable while they migrate to the
    // product-owned runtime root.
    localAppData ? joinWin(localAppData, 'hermes', 'git') : ''
  ].filter(Boolean)

  for (const gitRoot of managedGitRoots) {
    candidates.push(joinWin(gitRoot, 'bin', 'bash.exe'))
    candidates.push(joinWin(gitRoot, 'usr', 'bin', 'bash.exe'))
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
