import { createHash } from 'node:crypto'
import {
  chmodSync,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync
} from 'node:fs'
import path from 'node:path'

export const AUTH_TOOLCHAIN_SCHEMA_VERSION = 1
export const AUTH_TOOLCHAIN_PLATFORM = 'darwin'
export const AUTH_TOOLCHAIN_ARCH = 'arm64'

const SHA256_RE = /^[0-9a-f]{64}$/
const SAFE_VERSION_RE = /^[0-9]+(?:\.[0-9]+){1,3}$/
const FIXED_FILES = Object.freeze({
  uv: 'uv',
  python: 'python.tar.gz',
  requirements: 'auth-requirements.txt'
})

function sha256File(filePath) {
  return createHash('sha256').update(readFileSync(filePath)).digest('hex')
}

function requireRegularFile(filePath, label) {
  const stats = lstatSync(filePath)

  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link file: ${filePath}`)
  }

  return stats
}

function validateRelativeFile(value, label) {
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    value.includes('\\') ||
    value.includes('\0') ||
    path.posix.isAbsolute(value) ||
    path.posix.normalize(value) !== value ||
    value.split('/').includes('..')
  ) {
    throw new Error(`${label} file path is invalid`)
  }

  return value
}

function parseAsset(value, expectedFile, label) {
  if (!value || typeof value !== 'object') {
    throw new Error(`${label} metadata is invalid`)
  }
  if (value.file !== expectedFile) {
    throw new Error(`${label} file is invalid`)
  }
  if (!Number.isSafeInteger(value.size) || value.size <= 0) {
    throw new Error(`${label} size is invalid`)
  }
  if (typeof value.sha256 !== 'string' || !SHA256_RE.test(value.sha256)) {
    throw new Error(`${label} checksum is invalid`)
  }

  return value
}

function parseManifest(raw) {
  const value = typeof raw === 'string' ? JSON.parse(raw) : raw

  if (!value || typeof value !== 'object') {
    throw new Error('authentication toolchain manifest must be an object')
  }
  if (value.schemaVersion !== AUTH_TOOLCHAIN_SCHEMA_VERSION) {
    throw new Error(`authentication toolchain schemaVersion must be ${AUTH_TOOLCHAIN_SCHEMA_VERSION}`)
  }
  if (value.platform !== AUTH_TOOLCHAIN_PLATFORM || value.arch !== AUTH_TOOLCHAIN_ARCH) {
    throw new Error('authentication toolchain target must be darwin-arm64')
  }
  if (!SAFE_VERSION_RE.test(String(value.uv?.version || ''))) {
    throw new Error('authentication toolchain uv version is invalid')
  }
  if (!SAFE_VERSION_RE.test(String(value.python?.version || ''))) {
    throw new Error('authentication toolchain Python version is invalid')
  }

  parseAsset(value.uv, FIXED_FILES.uv, 'uv')
  parseAsset(value.python, FIXED_FILES.python, 'Python')
  parseAsset(value.requirements, FIXED_FILES.requirements, 'requirements')

  if (!Array.isArray(value.wheels) || value.wheels.length === 0) {
    throw new Error('authentication toolchain wheel list is empty')
  }

  const seen = new Set()
  for (const wheel of value.wheels) {
    const file = validateRelativeFile(wheel?.file, 'wheel')
    if (!file.startsWith('wheelhouse/') || seen.has(file)) {
      throw new Error('authentication toolchain wheel path is invalid or duplicated')
    }
    seen.add(file)
    parseAsset(wheel, file, 'wheel')
  }

  return value
}

function assetRecord(root, relativeFile, version) {
  const filePath = path.join(root, relativeFile)
  const stats = requireRegularFile(filePath, relativeFile)
  const record = {
    file: relativeFile,
    size: stats.size,
    sha256: sha256File(filePath)
  }

  return version ? { ...record, version } : record
}

function copyRegularFile(source, destination, label) {
  requireRegularFile(source, label)
  mkdirSync(path.dirname(destination), { recursive: true })
  copyFileSync(source, destination)
}

function installStagedDirectory(stagingRoot, outputDir) {
  const backupRoot = `${outputDir}.bak-${process.pid}`
  rmSync(backupRoot, { recursive: true, force: true })

  if (existsSync(outputDir)) {
    renameSync(outputDir, backupRoot)
  }

  try {
    renameSync(stagingRoot, outputDir)
    rmSync(backupRoot, { recursive: true, force: true })
  } catch (error) {
    if (!existsSync(outputDir) && existsSync(backupRoot)) {
      renameSync(backupRoot, outputDir)
    }
    throw error
  }
}

export function verifyAuthToolchain(root) {
  const manifestPath = path.join(root, 'manifest.json')
  requireRegularFile(manifestPath, 'authentication toolchain manifest')
  const manifest = parseManifest(readFileSync(manifestPath, 'utf8'))
  const assets = [manifest.uv, manifest.python, manifest.requirements, ...manifest.wheels]

  for (const asset of assets) {
    const relativeFile = validateRelativeFile(asset.file, 'asset')
    const filePath = path.join(root, relativeFile)
    const stats = requireRegularFile(filePath, `authentication toolchain asset ${relativeFile}`)

    if (stats.size !== asset.size) {
      throw new Error(`authentication toolchain asset size mismatch: ${relativeFile}`)
    }
    if (sha256File(filePath) !== asset.sha256) {
      throw new Error(`authentication toolchain asset checksum mismatch: ${relativeFile}`)
    }
  }

  return manifest
}

export function buildAuthToolchain(options) {
  const {
    outputDir,
    uvPath,
    pythonArchivePath,
    requirementsPath,
    wheelhousePath,
    platform,
    arch,
    uvVersion,
    pythonVersion
  } = options

  if (platform !== AUTH_TOOLCHAIN_PLATFORM || arch !== AUTH_TOOLCHAIN_ARCH) {
    throw new Error('authentication toolchain target must be darwin-arm64')
  }
  if (!SAFE_VERSION_RE.test(String(uvVersion || '')) || !SAFE_VERSION_RE.test(String(pythonVersion || ''))) {
    throw new Error('authentication toolchain versions are invalid')
  }

  requireRegularFile(uvPath, 'uv input')
  requireRegularFile(pythonArchivePath, 'Python archive input')
  requireRegularFile(requirementsPath, 'requirements input')
  const wheelEntries = readdirSync(wheelhousePath, { withFileTypes: true }).sort((a, b) =>
    a.name.localeCompare(b.name)
  )

  if (wheelEntries.length === 0) {
    throw new Error('authentication toolchain wheelhouse is empty')
  }

  const stagingRoot = `${outputDir}.tmp-${process.pid}`
  rmSync(stagingRoot, { recursive: true, force: true })
  mkdirSync(path.join(stagingRoot, 'wheelhouse'), { recursive: true })

  try {
    copyRegularFile(uvPath, path.join(stagingRoot, FIXED_FILES.uv), 'uv input')
    chmodSync(path.join(stagingRoot, FIXED_FILES.uv), 0o755)
    copyRegularFile(
      pythonArchivePath,
      path.join(stagingRoot, FIXED_FILES.python),
      'Python archive input'
    )
    copyRegularFile(
      requirementsPath,
      path.join(stagingRoot, FIXED_FILES.requirements),
      'requirements input'
    )

    for (const entry of wheelEntries) {
      const source = path.join(wheelhousePath, entry.name)
      const destination = path.join(stagingRoot, 'wheelhouse', entry.name)
      copyRegularFile(source, destination, `wheel ${entry.name}`)
    }

    const wheelFiles = wheelEntries.map(entry => `wheelhouse/${entry.name}`)
    const manifest = {
      schemaVersion: AUTH_TOOLCHAIN_SCHEMA_VERSION,
      platform,
      arch,
      uv: assetRecord(stagingRoot, FIXED_FILES.uv, uvVersion),
      python: assetRecord(stagingRoot, FIXED_FILES.python, pythonVersion),
      requirements: assetRecord(stagingRoot, FIXED_FILES.requirements),
      wheels: wheelFiles.map(file => assetRecord(stagingRoot, file))
    }

    writeFileSync(path.join(stagingRoot, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')
    verifyAuthToolchain(stagingRoot)
    installStagedDirectory(stagingRoot, outputDir)

    return { manifest, outputDir }
  } finally {
    rmSync(stagingRoot, { recursive: true, force: true })
  }
}
