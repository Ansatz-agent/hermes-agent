import { createHash } from 'node:crypto'
import type { Stats } from 'node:fs'
import fsp from 'node:fs/promises'
import path from 'node:path'

const MANIFEST_SCHEMA_VERSION = 1
const SHA256_RE = /^[0-9a-f]{64}$/
const VERSION_RE = /^[0-9]+(?:\.[0-9]+){1,3}$/
type AuthToolchainTarget =
  | { platform: 'darwin'; arch: 'arm64'; uvFile: 'uv.gz'; pythonFile: 'python.tar.gz' }
  | { platform: 'win32'; arch: 'x64'; uvFile: 'uv.exe'; pythonFile: 'python-embed.zip' }

const TARGETS: Record<string, AuthToolchainTarget> = {
  'darwin-arm64': { platform: 'darwin', arch: 'arm64', uvFile: 'uv.gz', pythonFile: 'python.tar.gz' },
  'win32-x64': { platform: 'win32', arch: 'x64', uvFile: 'uv.exe', pythonFile: 'python-embed.zip' }
}

type ToolchainAsset = { file: string; size: number; sha256: string }
type VersionedToolchainAsset = ToolchainAsset & { version: string }

export interface AuthToolchainManifest {
  schemaVersion: 1
  platform: 'darwin' | 'win32'
  arch: 'arm64' | 'x64'
  uv: VersionedToolchainAsset
  python: VersionedToolchainAsset
  requirements: ToolchainAsset
  wheels: ToolchainAsset[]
}

export interface BundledAuthToolchain {
  root: string
  manifest: AuthToolchainManifest
  manifestPath: string
  uvAssetPath: string
  uvArchivePath: string
  pythonArchivePath: string
  requirementsPath: string
  wheelPaths: string[]
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function validateRelativeFile(value: unknown, label: string): string {
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

function parseAsset(value: unknown, expectedFile: string | null, label: string): ToolchainAsset {
  if (!value || typeof value !== 'object') {
    throw new Error(`${label} metadata is invalid`)
  }

  const asset = value as Record<string, unknown>
  const file = validateRelativeFile(asset.file, label)

  if (expectedFile !== null && file !== expectedFile) {
    throw new Error(`${label} file is invalid`)
  }

  if (!isPositiveSafeInteger(asset.size)) {
    throw new Error(`${label} size is invalid`)
  }

  if (typeof asset.sha256 !== 'string' || !SHA256_RE.test(asset.sha256)) {
    throw new Error(`${label} checksum is invalid`)
  }

  return asset as ToolchainAsset
}

function parseVersionedAsset(value: unknown, expectedFile: string, label: string): VersionedToolchainAsset {
  const asset = parseAsset(value, expectedFile, label) as VersionedToolchainAsset

  if (typeof asset.version !== 'string' || !VERSION_RE.test(asset.version)) {
    throw new Error(`${label} version is invalid`)
  }

  return asset
}

function targetFor(platform: unknown, arch: unknown): AuthToolchainTarget {
  const target = TARGETS[`${String(platform)}-${String(arch)}`]

  if (!target) {
    throw new Error(`unsupported authentication toolchain target: ${String(platform)}-${String(arch)}`)
  }

  return target
}

function parseManifest(raw: unknown): AuthToolchainManifest {
  if (!raw || typeof raw !== 'object') {
    throw new Error('authentication toolchain manifest must be an object')
  }

  const value = raw as Record<string, unknown>

  if (value.schemaVersion !== MANIFEST_SCHEMA_VERSION) {
    throw new Error(`authentication toolchain schemaVersion must be ${MANIFEST_SCHEMA_VERSION}`)
  }

  const target = targetFor(value.platform, value.arch)

  const uv = parseVersionedAsset(value.uv, target.uvFile, 'uv')
  const python = parseVersionedAsset(value.python, target.pythonFile, 'Python')
  const requirements = parseAsset(value.requirements, 'auth-requirements.txt', 'requirements')

  if (!Array.isArray(value.wheels) || value.wheels.length === 0) {
    throw new Error('authentication toolchain wheel list is empty')
  }

  const seen = new Set<string>()

  const wheels = value.wheels.map((candidate, index) => {
    const wheel = parseAsset(candidate, null, `wheel ${index + 1}`)

    if (!wheel.file.startsWith('wheelhouse/') || seen.has(wheel.file)) {
      throw new Error('wheel file path is invalid or duplicated')
    }

    seen.add(wheel.file)

    return wheel
  })

  return {
    schemaVersion: 1,
    platform: target.platform,
    arch: target.arch,
    uv,
    python,
    requirements,
    wheels
  }
}

async function requireRegularFile(filePath: string, label: string): Promise<Stats> {
  let stats: Stats

  try {
    stats = await fsp.lstat(filePath)
  } catch (error) {
    throw new Error(`Missing ${label} at ${filePath}: ${(error as Error).message}`)
  }

  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link file: ${filePath}`)
  }

  return stats
}

async function requireRegularDirectory(directoryPath: string, label: string): Promise<void> {
  const stats = await fsp.lstat(directoryPath)

  if (!stats.isDirectory() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link directory: ${directoryPath}`)
  }
}

async function sha256File(filePath: string): Promise<string> {
  return createHash('sha256')
    .update(await fsp.readFile(filePath))
    .digest('hex')
}

function assetPath(root: string, relativeFile: string): string {
  const resolvedRoot = path.resolve(root)
  const resolved = path.resolve(resolvedRoot, relativeFile)

  if (!resolved.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new Error('authentication toolchain asset escapes its root')
  }

  return resolved
}

export async function resolveBundledAuthToolchain(
  root: string,
  target = { platform: process.platform, arch: process.arch }
): Promise<BundledAuthToolchain> {
  const requestedTarget = targetFor(target.platform, target.arch)

  await requireRegularDirectory(root, 'authentication toolchain root')
  const manifestPath = path.join(root, 'manifest.json')
  await requireRegularFile(manifestPath, 'authentication toolchain manifest')

  let raw: unknown

  try {
    raw = JSON.parse(await fsp.readFile(manifestPath, 'utf8'))
  } catch (error) {
    throw new Error(`Cannot read authentication toolchain manifest: ${(error as Error).message}`)
  }

  const manifest = parseManifest(raw)

  if (manifest.platform !== requestedTarget.platform || manifest.arch !== requestedTarget.arch) {
    throw new Error(
      `authentication toolchain target mismatch: expected ${requestedTarget.platform}-${requestedTarget.arch}`
    )
  }

  const assets = [manifest.uv, manifest.python, manifest.requirements, ...manifest.wheels]

  for (const asset of assets) {
    const filePath = assetPath(root, asset.file)
    const stats = await requireRegularFile(filePath, `authentication toolchain asset ${asset.file}`)

    if (stats.size !== asset.size) {
      throw new Error(`authentication toolchain asset size mismatch: ${asset.file}`)
    }

    if ((await sha256File(filePath)) !== asset.sha256) {
      throw new Error(`authentication toolchain asset checksum mismatch: ${asset.file}`)
    }
  }

  return {
    root: path.resolve(root),
    manifest,
    manifestPath,
    uvAssetPath: assetPath(root, manifest.uv.file),
    uvArchivePath: assetPath(root, manifest.uv.file),
    pythonArchivePath: assetPath(root, manifest.python.file),
    requirementsPath: assetPath(root, manifest.requirements.file),
    wheelPaths: manifest.wheels.map(wheel => assetPath(root, wheel.file))
  }
}
