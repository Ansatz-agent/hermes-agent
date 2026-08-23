import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const UV_VERSION = '0.12.5'
const PYTHON_ROOT_RE = /^cpython-3\.11\.[0-9]+-macos-aarch64-none$/
const VERSION_RE = /^[0-9]+(?:\.[0-9]+){2}$/
const PYTHON_PRIMARY = 'https://mirrors.ustc.edu.cn/pypi/simple'
const PYTHON_FALLBACK = 'https://pypi.tuna.tsinghua.edu.cn/simple'

const SAFE_ENV_KEYS = Object.freeze([
  'APPDATA', 'HOME', 'LANG', 'LC_ALL', 'LOCALAPPDATA', 'PATH', 'SYSTEMROOT',
  'TEMP', 'TMP', 'TMPDIR', 'USER', 'USERPROFILE', 'UV_PYTHON_INSTALL_DIR', 'WINDIR'
])

export function buildAuthPayloadEnvironment(source = process.env) {
  const env = {}
  for (const key of SAFE_ENV_KEYS) {
    if (typeof source[key] === 'string') env[key] = source[key]
  }
  env.UV_NO_CONFIG = '1'
  env.UV_DEFAULT_INDEX = PYTHON_PRIMARY
  env.UV_INDEX = PYTHON_PRIMARY
  env.PIP_CONFIG_FILE = source.SYSTEMROOT || source.WINDIR ? 'NUL' : '/dev/null'
  env.PIP_INDEX_URL = PYTHON_PRIMARY
  env.PIP_DISABLE_PIP_VERSION_CHECK = '1'
  env.PIP_NO_INPUT = '1'
  env.HERMES_UV_FALLBACK_INDEX = PYTHON_FALLBACK
  env.HF_HUB_OFFLINE = '1'
  env.HF_HUB_DISABLE_TELEMETRY = '1'
  env.CI = '1'
  return env
}

export function buildAuthLockEnvironment(source = process.env) {
  const env = buildAuthPayloadEnvironment(source)

  delete env.UV_DEFAULT_INDEX
  delete env.UV_INDEX
  delete env.PIP_INDEX_URL
  delete env.HERMES_UV_FALLBACK_INDEX
  env.UV_OFFLINE = '1'

  return env
}

export const WINDOWS_PYTHON_VERSION = '3.13.15'
export const WINDOWS_PYTHON_ARCHIVE = 'python-3.13.15-embed-amd64.zip'
export const WINDOWS_PYTHON_SHA256 = 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'
export const WINDOWS_PYTHON_SOURCES = Object.freeze([
  `https://mirrors.huaweicloud.com/python/${WINDOWS_PYTHON_VERSION}/${WINDOWS_PYTHON_ARCHIVE}`,
  `https://www.python.org/ftp/python/${WINDOWS_PYTHON_VERSION}/${WINDOWS_PYTHON_ARCHIVE}`
])
export const WINDOWS_UV_VERSION = UV_VERSION
export const WINDOWS_UV_WHEEL = `uv-${WINDOWS_UV_VERSION}-py3-none-win_amd64.whl`
export const WINDOWS_UV_SHA256 = '455c3e57602e2141e66e2f0bf685898c9c5e5a70377d14c9a71554a3baf3ddbf'

function defaultExecute(command, args, options = {}) {
  return execFileSync(command, args, {
    encoding: 'utf8',
    env: options.env || buildAuthPayloadEnvironment(),
    maxBuffer: 16 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe']
  })
}

function defaultSha256File(filePath) {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

async function defaultDownloadFile({ sources, destination, expectedSha256 }) {
  let lastError = null

  for (const source of sources) {
    fs.rmSync(destination, { force: true })
    try {
      defaultExecute(process.platform === 'win32' ? 'curl.exe' : '/usr/bin/curl', [
        '-fL',
        '--connect-timeout',
        '30',
        '--max-time',
        '600',
        '--retry',
        '2',
        '--output',
        destination,
        source
      ])
      const observed = defaultSha256File(destination)
      if (observed !== expectedSha256) {
        throw new Error(`SHA-256 mismatch: expected ${expectedSha256}, got ${observed}`)
      }

      return { source, sha256: observed }
    } catch (error) {
      lastError = error
    }
  }

  fs.rmSync(destination, { force: true })
  throw new Error(`all approved download sources failed: ${lastError?.message || String(lastError)}`)
}

function windowsPowerShellPath(env = process.env) {
  const systemRoot = env.SystemRoot || env.SYSTEMROOT || env.windir || env.WINDIR || 'C:\\Windows'
  return path.win32.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

function defaultExtractUvExecutable({ archivePath, destination }) {
  if (process.platform === 'win32') {
    const extractionRoot = `${archivePath}.extract-${process.pid}`
    fs.rmSync(extractionRoot, { recursive: true, force: true })
    fs.mkdirSync(extractionRoot, { recursive: true })
    try {
      defaultExecute(windowsPowerShellPath(), [
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        'Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force',
        archivePath,
        extractionRoot
      ])
      const candidates = []
      const visit = directory => {
        for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
          const candidate = path.join(directory, entry.name)
          if (entry.isDirectory()) visit(candidate)
          else if (entry.isFile() && entry.name.toLowerCase() === 'uv.exe') candidates.push(candidate)
        }
      }
      visit(extractionRoot)
      if (candidates.length !== 1) throw new Error('uv wheel must contain exactly one uv.exe')
      fs.copyFileSync(candidates[0], destination)
    } finally {
      fs.rmSync(extractionRoot, { recursive: true, force: true })
    }
    return
  }

  const listing = String(defaultExecute('/usr/bin/unzip', ['-Z1', archivePath]))
    .split(/\r?\n/)
    .filter(candidate => /(^|\/)uv\.exe$/i.test(candidate))
  if (listing.length !== 1) throw new Error('uv wheel must contain exactly one uv.exe')
  const contents = execFileSync('/usr/bin/unzip', ['-p', archivePath, listing[0]], {
    encoding: null,
    maxBuffer: 64 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe']
  })
  fs.writeFileSync(destination, contents)
}

function requireRegularFile(filePath, label) {
  const stats = fs.lstatSync(filePath)

  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link file: ${filePath}`)
  }

  return filePath
}

function requireMacArm64Executable(filePath, label, execute) {
  const description = String(execute('/usr/bin/file', ['-b', filePath])).trim()

  if (!/Mach-O 64-bit executable arm64/.test(description) || /x86_64/.test(description)) {
    throw new Error(`${label} executable must be macOS arm64`)
  }
}

function replaceDirectory(stagingRoot, outputDir) {
  const backupRoot = `${outputDir}.bak-${process.pid}`

  fs.rmSync(backupRoot, { recursive: true, force: true })
  if (fs.existsSync(outputDir)) {
    fs.renameSync(outputDir, backupRoot)
  }

  try {
    fs.renameSync(stagingRoot, outputDir)
    fs.rmSync(backupRoot, { recursive: true, force: true })
  } catch (error) {
    if (!fs.existsSync(outputDir) && fs.existsSync(backupRoot)) {
      fs.renameSync(backupRoot, outputDir)
    }
    throw error
  }
}

function downloadLockedWheels({ execute, pythonPath, requirementsPath, wheelhousePath, indexUrl }) {
  return execute(pythonPath, [
    '-m',
    'pip',
    'download',
    '--require-hashes',
    '--only-binary=:all:',
    '--dest',
    wheelhousePath,
    '--requirement',
    requirementsPath,
    '--index-url',
    indexUrl,
    '--disable-pip-version-check',
    '--no-input'
  ], { env: { ...buildAuthPayloadEnvironment(), UV_DEFAULT_INDEX: indexUrl, PIP_INDEX_URL: indexUrl } })
}

function downloadWindowsWheels({ execute, pythonPath, requirementsPath, wheelhousePath, indexUrl }) {
  return execute(pythonPath, [
    '-m',
    'pip',
    'download',
    '--require-hashes',
    '--only-binary=:all:',
    '--platform',
    'win_amd64',
    '--python-version',
    '313',
    '--implementation',
    'cp',
    '--dest',
    wheelhousePath,
    '--requirement',
    requirementsPath,
    '--index-url',
    indexUrl,
    '--disable-pip-version-check',
    '--no-input'
  ], { env: { ...buildAuthPayloadEnvironment(), UV_DEFAULT_INDEX: indexUrl, PIP_INDEX_URL: indexUrl } })
}

function normalizedDistributionName(value) {
  return value.toLowerCase().replace(/[._-]+/g, '-')
}

function lockedRequirementNames(requirements) {
  const names = new Set()
  for (const line of requirements.split(/\r?\n/)) {
    const match = /^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==/.exec(line.trim())
    if (match) names.add(normalizedDistributionName(match[1]))
  }
  return names
}

function validateWindowsWheelhouse(wheelhousePath, requirements) {
  const authorized = lockedRequirementNames(requirements)
  const entries = fs.readdirSync(wheelhousePath, { withFileTypes: true })
  if (entries.length === 0) throw new Error('Windows authentication wheelhouse is empty')

  for (const entry of entries) {
    if (!entry.isFile() || (!/-none-any\.whl$/i.test(entry.name) && !/-win_amd64\.whl$/i.test(entry.name))) {
      throw new Error(`authentication wheel is not Windows x64 compatible: ${entry.name}`)
    }
    const distribution = normalizedDistributionName(entry.name.split('-')[0])
    if (!authorized.has(distribution)) {
      throw new Error(`authentication wheel is not authorized by the lock export: ${entry.name}`)
    }
  }
}

function downloadWindowsUvWheel({ execute, pythonPath, destinationRoot }) {
  let lastError = null
  for (const indexUrl of [PYTHON_PRIMARY, PYTHON_FALLBACK]) {
    fs.rmSync(destinationRoot, { recursive: true, force: true })
    fs.mkdirSync(destinationRoot, { recursive: true })
    try {
      execute(pythonPath, [
        '-m',
        'pip',
        'download',
        `uv==${WINDOWS_UV_VERSION}`,
        '--no-deps',
        '--only-binary=:all:',
        '--platform',
        'win_amd64',
        '--python-version',
        '313',
        '--implementation',
        'cp',
        '--dest',
        destinationRoot,
        '--index-url',
        indexUrl,
        '--disable-pip-version-check',
        '--no-input'
      ], { env: { ...buildAuthPayloadEnvironment(), UV_DEFAULT_INDEX: indexUrl, PIP_INDEX_URL: indexUrl } })
      const wheelPath = path.join(destinationRoot, WINDOWS_UV_WHEEL)
      requireRegularFile(wheelPath, 'Windows uv wheel')
      return { indexUrl, wheelPath }
    } catch (error) {
      lastError = error
    }
  }
  throw new Error(`Windows uv wheel download failed from approved mirrors: ${lastError?.message || String(lastError)}`)
}

export async function prepareWindowsAuthToolchainInputs({
  execute = defaultExecute,
  outputDir,
  projectRoot,
  hostUvPath,
  hostPythonPath,
  downloadFile = defaultDownloadFile,
  sha256File = defaultSha256File,
  extractUvExecutable = defaultExtractUvExecutable
}) {
  if (![outputDir, projectRoot, hostUvPath, hostPythonPath].every(value => path.isAbsolute(value))) {
    throw new Error('Windows authentication toolchain preparation paths must be absolute')
  }
  requireRegularFile(hostUvPath, 'host uv')
  requireRegularFile(hostPythonPath, 'host Python')

  const authProject = path.join(projectRoot, 'desktop_auth_runtime')
  const authUvConfig = path.join(authProject, 'uv.toml')
  requireRegularFile(path.join(authProject, 'pyproject.toml'), 'authentication pyproject')
  requireRegularFile(path.join(authProject, 'uv.lock'), 'authentication lock')
  requireRegularFile(authUvConfig, 'authentication uv config')

  const stagingRoot = `${outputDir}.tmp-${process.pid}`
  const wheelhousePath = path.join(stagingRoot, 'wheelhouse')
  const uvDownloadRoot = path.join(stagingRoot, 'uv-wheel')
  const requirementsPath = path.join(stagingRoot, 'auth-requirements.txt')
  const pythonArchivePath = path.join(stagingRoot, 'python-embed.zip')
  const stagedUvPath = path.join(stagingRoot, 'uv.exe')
  fs.rmSync(stagingRoot, { recursive: true, force: true })
  fs.mkdirSync(wheelhousePath, { recursive: true })

  try {
    const pythonDownload = await downloadFile({
      sources: WINDOWS_PYTHON_SOURCES,
      destination: pythonArchivePath,
      expectedSha256: WINDOWS_PYTHON_SHA256,
      label: 'CPython Windows embeddable x64'
    })
    requireRegularFile(pythonArchivePath, 'Windows Python archive')
    if (pythonDownload?.sha256 !== WINDOWS_PYTHON_SHA256 || sha256File(pythonArchivePath) !== WINDOWS_PYTHON_SHA256) {
      throw new Error('Windows Python archive SHA-256 mismatch')
    }

    execute(hostUvPath, [
      'export',
      '--project',
      authProject,
      '--locked',
      '--no-dev',
      '--no-emit-project',
      '--format',
      'requirements-txt',
      '--output-file',
      requirementsPath,
      '--config-file',
      authUvConfig
    ], { env: buildAuthLockEnvironment() })
    const requirements = fs.readFileSync(requirementsPath, 'utf8')
    if (!requirements.includes('--hash=sha256:')) {
      throw new Error('authentication requirements export is not hash locked')
    }
    if (/github\.com|raw\.githubusercontent\.com|git\+|file:/i.test(requirements)) {
      throw new Error('authentication requirements export contains a forbidden source')
    }

    const uvDownload = downloadWindowsUvWheel({
      execute,
      pythonPath: hostPythonPath,
      destinationRoot: uvDownloadRoot
    })
    if (sha256File(uvDownload.wheelPath) !== WINDOWS_UV_SHA256) {
      throw new Error('Windows uv wheel SHA-256 mismatch')
    }
    extractUvExecutable({ archivePath: uvDownload.wheelPath, destination: stagedUvPath })
    requireRegularFile(stagedUvPath, 'extracted Windows uv executable')

    let wheelIndex = null
    let wheelError = null
    for (const indexUrl of [PYTHON_PRIMARY, PYTHON_FALLBACK]) {
      fs.rmSync(wheelhousePath, { recursive: true, force: true })
      fs.mkdirSync(wheelhousePath, { recursive: true })
      try {
        downloadWindowsWheels({
          execute,
          pythonPath: hostPythonPath,
          requirementsPath,
          wheelhousePath,
          indexUrl
        })
        validateWindowsWheelhouse(wheelhousePath, requirements)
        wheelIndex = indexUrl
        break
      } catch (error) {
        wheelError = error
      }
    }
    if (!wheelIndex) {
      throw wheelError || new Error('Windows authentication wheels could not be prepared')
    }

    const metadata = {
      schemaVersion: 1,
      platform: 'win32',
      arch: 'x64',
      uvVersion: WINDOWS_UV_VERSION,
      pythonVersion: WINDOWS_PYTHON_VERSION,
      pythonSource: pythonDownload.source,
      uvIndex: uvDownload.indexUrl,
      pythonIndexes: [PYTHON_PRIMARY, PYTHON_FALLBACK],
      selectedWheelIndex: wheelIndex
    }
    fs.writeFileSync(path.join(stagingRoot, 'metadata.json'), `${JSON.stringify(metadata, null, 2)}\n`)
    fs.rmSync(uvDownloadRoot, { recursive: true, force: true })
    replaceDirectory(stagingRoot, outputDir)

    return {
      ...metadata,
      outputDir,
      uvPath: path.join(outputDir, 'uv.exe'),
      pythonArchivePath: path.join(outputDir, 'python-embed.zip'),
      requirementsPath: path.join(outputDir, 'auth-requirements.txt'),
      wheelhousePath: path.join(outputDir, 'wheelhouse')
    }
  } finally {
    fs.rmSync(stagingRoot, { recursive: true, force: true })
  }
}

export function prepareAuthToolchainInputs({
  execute = defaultExecute,
  outputDir,
  projectRoot,
  pythonPath,
  uvPath
}) {
  if (![outputDir, projectRoot, pythonPath, uvPath].every(value => path.isAbsolute(value))) {
    throw new Error('authentication toolchain preparation paths must be absolute')
  }

  requireRegularFile(uvPath, 'uv')
  requireRegularFile(pythonPath, 'Python')
  requireMacArm64Executable(uvPath, 'uv', execute)
  requireMacArm64Executable(pythonPath, 'Python', execute)

  const uvVersionMatch = String(execute(uvPath, ['--version'])).match(/^uv ([0-9]+(?:\.[0-9]+){2})\b/)
  const uvVersion = uvVersionMatch?.[1] || ''
  const pythonVersion = String(
    execute(pythonPath, ['-c', 'import platform; print(platform.python_version())'])
  ).trim()

  if (uvVersion !== UV_VERSION) {
    throw new Error(`uv ${UV_VERSION} is required, got ${uvVersion || 'unknown'}`)
  }
  if (!VERSION_RE.test(pythonVersion) || !pythonVersion.startsWith('3.11.')) {
    throw new Error(`CPython 3.11 is required, got ${pythonVersion || 'unknown'}`)
  }

  const pythonRoot = path.dirname(path.dirname(pythonPath))
  const pythonRootName = path.basename(pythonRoot)

  if (!PYTHON_ROOT_RE.test(pythonRootName)) {
    throw new Error('Python must be the relocatable macOS arm64 uv runtime')
  }

  const authProject = path.join(projectRoot, 'desktop_auth_runtime')
  const authUvConfig = path.join(authProject, 'uv.toml')
  requireRegularFile(path.join(authProject, 'pyproject.toml'), 'authentication pyproject')
  requireRegularFile(path.join(authProject, 'uv.lock'), 'authentication lock')
  requireRegularFile(authUvConfig, 'authentication uv config')

  const stagingRoot = `${outputDir}.tmp-${process.pid}`
  const wheelhousePath = path.join(stagingRoot, 'wheelhouse')
  const requirementsPath = path.join(stagingRoot, 'auth-requirements.txt')
  const pythonArchivePath = path.join(stagingRoot, 'python.tar.gz')
  const stagedUvPath = path.join(stagingRoot, 'uv')

  fs.rmSync(stagingRoot, { recursive: true, force: true })
  fs.mkdirSync(wheelhousePath, { recursive: true })

  try {
    fs.copyFileSync(uvPath, stagedUvPath)
    fs.chmodSync(stagedUvPath, 0o755)
    execute('/usr/bin/tar', [
      '-czf',
      pythonArchivePath,
      '-C',
      path.dirname(pythonRoot),
      pythonRootName
    ])
    execute(uvPath, [
      'export',
      '--project',
      authProject,
      '--locked',
      '--no-dev',
      '--no-emit-project',
      '--format',
      'requirements-txt',
      '--output-file',
      requirementsPath,
      '--config-file',
      authUvConfig
    ], { env: buildAuthLockEnvironment() })

    const requirements = fs.readFileSync(requirementsPath, 'utf8')
    if (!requirements.includes('--hash=sha256:')) {
      throw new Error('authentication requirements export is not hash locked')
    }
    if (/github\.com|raw\.githubusercontent\.com|git\+|file:/i.test(requirements)) {
      throw new Error('authentication requirements export contains a forbidden source')
    }

    try {
      downloadLockedWheels({
        execute,
        pythonPath,
        requirementsPath,
        wheelhousePath,
        indexUrl: PYTHON_PRIMARY
      })
    } catch {
      fs.rmSync(wheelhousePath, { recursive: true, force: true })
      fs.mkdirSync(wheelhousePath, { recursive: true })
      downloadLockedWheels({
        execute,
        pythonPath,
        requirementsPath,
        wheelhousePath,
        indexUrl: PYTHON_FALLBACK
      })
    }

    const wheels = fs.readdirSync(wheelhousePath, { withFileTypes: true })
    if (wheels.length === 0 || wheels.some(entry => !entry.isFile() || !entry.name.endsWith('.whl'))) {
      throw new Error('authentication wheelhouse must contain only wheels')
    }

    requireRegularFile(pythonArchivePath, 'Python archive')
    const metadata = {
      schemaVersion: 1,
      platform: 'darwin',
      arch: 'arm64',
      uvVersion,
      pythonVersion,
      pythonIndexes: [PYTHON_PRIMARY, PYTHON_FALLBACK]
    }
    fs.writeFileSync(path.join(stagingRoot, 'metadata.json'), `${JSON.stringify(metadata, null, 2)}\n`)
    replaceDirectory(stagingRoot, outputDir)

    return {
      ...metadata,
      outputDir,
      uvPath: path.join(outputDir, 'uv'),
      pythonArchivePath: path.join(outputDir, 'python.tar.gz'),
      requirementsPath: path.join(outputDir, 'auth-requirements.txt'),
      wheelhousePath: path.join(outputDir, 'wheelhouse')
    }
  } finally {
    fs.rmSync(stagingRoot, { recursive: true, force: true })
  }
}

export function findUv(
  env,
  projectRoot,
  {
    platform = process.platform,
    execute = defaultExecute,
    existsSync = fs.existsSync,
    homeDir = os.homedir()
  } = {}
) {
  const pathApi = platform === 'win32' ? path.win32 : path
  const executable = platform === 'win32' ? 'uv.exe' : 'uv'
  const userHome = platform === 'win32' ? env.USERPROFILE : (env.HOME || homeDir)
  const candidates = [
    env.HERMES_AUTH_TOOLCHAIN_UV_PATH,
    env.HERMES_HOME ? pathApi.join(env.HERMES_HOME, 'bin', executable) : null,
    userHome ? pathApi.join(userHome, '.hermes', 'bin', executable) : null
  ].filter(Boolean)

  for (const candidate of candidates) {
    if (pathApi.isAbsolute(candidate) && existsSync(candidate)) {
      return candidate
    }
  }

  const pathValue = Object.entries(env).find(([key]) => key.toUpperCase() === 'PATH')?.[1]
  if (typeof pathValue === 'string') {
    for (const rawDirectory of pathValue.split(pathApi.delimiter)) {
      const directory = rawDirectory.trim().replace(/^"(.*)"$/, '$1')
      const candidate = pathApi.join(directory, executable)
      if (pathApi.isAbsolute(candidate) && existsSync(candidate)) return candidate
    }
  }

  try {
    const command = platform === 'win32' ? 'where.exe' : '/usr/bin/which'
    for (const candidate of String(execute(command, ['uv'])).split(/\r?\n/).filter(Boolean)) {
      if (pathApi.isAbsolute(candidate) && existsSync(candidate)) return candidate
    }
  } catch {
    void 0
  }

  throw new Error(`uv ${UV_VERSION} was not found for the ${platform} package build at ${projectRoot}`)
}

export function findManagedPython(uvPath, version, execute = defaultExecute) {
  const candidate = String(execute(uvPath, [
    'python',
    'find',
    '--no-project',
    '--managed-python',
    '--resolve-links',
    version
  ])).trim()

  if (!path.isAbsolute(candidate)) {
    throw new Error(`uv-managed Python ${version} was not found`)
  }

  return candidate
}

export async function prepareWindowsAuthToolchainInputsFromEnvironment(env = process.env) {
  const moduleDir = path.dirname(fileURLToPath(import.meta.url))
  const projectRoot = path.resolve(moduleDir, '../../..')
  const outputDir = path.resolve(
    env.HERMES_AUTH_TOOLCHAIN_INPUT_DIR || path.join(projectRoot, 'apps/desktop/build/auth-toolchain-inputs-win32-x64')
  )
  const hostUvPath = findUv(env, projectRoot)
  const hostPythonPath = env.HERMES_AUTH_TOOLCHAIN_HOST_PYTHON
    ? path.resolve(env.HERMES_AUTH_TOOLCHAIN_HOST_PYTHON)
    : findManagedPython(hostUvPath, '3.13')

  return prepareWindowsAuthToolchainInputs({
    outputDir,
    projectRoot,
    hostUvPath,
    hostPythonPath
  })
}

export function prepareAuthToolchainInputsFromEnvironment(env = process.env) {
  const moduleDir = path.dirname(fileURLToPath(import.meta.url))
  const projectRoot = path.resolve(moduleDir, '../../..')
  const outputDir = path.resolve(
    env.HERMES_AUTH_TOOLCHAIN_INPUT_DIR || path.join(projectRoot, 'apps/desktop/build/auth-toolchain-inputs')
  )
  const uvPath = findUv(env, projectRoot)
  const pythonPath = env.HERMES_AUTH_TOOLCHAIN_PYTHON_BIN
    ? path.resolve(env.HERMES_AUTH_TOOLCHAIN_PYTHON_BIN)
    : findManagedPython(uvPath, '3.11')

  return prepareAuthToolchainInputs({ outputDir, projectRoot, pythonPath, uvPath })
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : null
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    const targetPlatform = process.argv.includes('--platform')
      ? process.argv[process.argv.indexOf('--platform') + 1]
      : process.platform
    const result = targetPlatform === 'win32'
      ? await prepareWindowsAuthToolchainInputsFromEnvironment()
      : prepareAuthToolchainInputsFromEnvironment()
    process.stdout.write(`${JSON.stringify(result)}\n`)
  } catch (error) {
    process.stderr.write(`Hermes auth toolchain preparation failed: ${error.message || String(error)}\n`)
    process.exitCode = 1
  }
}
