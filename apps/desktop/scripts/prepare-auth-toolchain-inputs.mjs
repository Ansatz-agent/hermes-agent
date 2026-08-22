import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const UV_VERSION = '0.12.5'
const PYTHON_ROOT_RE = /^cpython-3\.11\.[0-9]+-macos-aarch64-none$/
const VERSION_RE = /^[0-9]+(?:\.[0-9]+){2}$/
const PYTHON_PRIMARY = 'https://mirrors.ustc.edu.cn/pypi/simple'
const PYTHON_FALLBACK = 'https://pypi.tuna.tsinghua.edu.cn/simple'

function defaultExecute(command, args) {
  return execFileSync(command, args, {
    encoding: 'utf8',
    maxBuffer: 16 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe']
  })
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
  ])
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
  requireRegularFile(path.join(authProject, 'pyproject.toml'), 'authentication pyproject')
  requireRegularFile(path.join(authProject, 'uv.lock'), 'authentication lock')

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
      requirementsPath
    ])

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

function findUv(env, projectRoot) {
  const candidates = [
    env.HERMES_AUTH_TOOLCHAIN_UV_PATH,
    env.HERMES_HOME ? path.join(env.HERMES_HOME, 'bin', 'uv') : null,
    path.join(os.homedir(), '.hermes', 'bin', 'uv')
  ].filter(Boolean)

  for (const candidate of candidates) {
    if (path.isAbsolute(candidate) && fs.existsSync(candidate)) {
      return candidate
    }
  }

  try {
    const candidate = String(defaultExecute('/usr/bin/which', ['uv'])).trim()
    if (path.isAbsolute(candidate) && fs.existsSync(candidate)) {
      return candidate
    }
  } catch {
    void 0
  }

  throw new Error(`uv ${UV_VERSION} was not found for the DMG build at ${projectRoot}`)
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
    : String(defaultExecute(uvPath, ['python', 'find', '3.11'])).trim()

  return prepareAuthToolchainInputs({ outputDir, projectRoot, pythonPath, uvPath })
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : null
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    const result = prepareAuthToolchainInputsFromEnvironment()
    process.stdout.write(`${JSON.stringify(result)}\n`)
  } catch (error) {
    process.stderr.write(`Hermes auth toolchain preparation failed: ${error.message || String(error)}\n`)
    process.exitCode = 1
  }
}
