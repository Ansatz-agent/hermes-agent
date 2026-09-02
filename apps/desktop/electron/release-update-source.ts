const UPDATE_BASE_URL_ENV = 'ANSATZ_UPDATE_BASE_URL'
const DEFAULT_UPDATE_BASE_URL = 'https://setup.hermes-agent.nousresearch.com'
const RELEASE_REPOSITORY = 'Ansatz-agent/hermes-agent'
const LATEST_RELEASE_PATH = `/repos/${RELEASE_REPOSITORY}/releases/latest`
const SOURCE_ARCHIVE_ASSET_NAME = 'hermes-backend.tar.gz'
const COMMIT_RE = /^[0-9a-f]{40}$/i
const SHA256_RE = /^[0-9a-f]{64}$/
const MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024

interface ReleaseArchive {
  url: string
  size: number
  sha256: string
}

export interface AnsatzReleaseMetadata {
  version: string
  commit: string
  channel: string
  archive: ReleaseArchive
  publishedAt?: string
}

function resolveUpdateBaseUrl(environment: NodeJS.ProcessEnv = process.env): string {
  const value = (environment[UPDATE_BASE_URL_ENV] || DEFAULT_UPDATE_BASE_URL).trim().replace(/\/+$/, '')
  const parsed = new URL(value)

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(`${UPDATE_BASE_URL_ENV} must be an absolute http(s) URL`)
  }

  return value
}

function platformName(platform: NodeJS.Platform): string {
  if (platform === 'win32') {return 'windows'}

  if (platform === 'darwin') {return 'macos'}

  return 'linux'
}

function architectureName(architecture: string): string {
  if (architecture === 'x64' || architecture === 'amd64') {return 'x64'}

  if (architecture === 'arm64' || architecture === 'aarch64') {return 'arm64'}

  return architecture
}

function sourceAssetNames(platform: NodeJS.Platform, architecture: string): string[] {
  return [
    `hermes-backend-${platformName(platform)}-${architectureName(architecture)}.tar.gz`,
    SOURCE_ARCHIVE_ASSET_NAME
  ]
}

function validateReleaseMetadata(
  raw: unknown,
  baseUrl: string,
  {
    platform = process.platform,
    architecture = process.arch
  }: { platform?: NodeJS.Platform; architecture?: string } = {}
): AnsatzReleaseMetadata {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('release metadata must be a JSON object')
  }

  const value = raw as Record<string, any>

  if (value.draft !== false || value.prerelease !== false) {
    throw new Error('latest release must be published and stable')
  }

  const tagName = typeof value.tag_name === 'string' ? value.tag_name.trim() : ''
  const version = /^[vV]/.test(tagName) ? tagName.slice(1) : tagName

  if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version)) {
    throw new Error('release metadata tag_name is invalid')
  }

  if (
    typeof value.target_commitish !== 'string' ||
    !COMMIT_RE.test(value.target_commitish) ||
    /^0+$/.test(value.target_commitish)
  ) {
    throw new Error('release metadata target_commitish must be a real Git SHA')
  }

  const names = sourceAssetNames(platform, architecture)
  const assets = Array.isArray(value.assets) ? value.assets : null
  const archive = assets
    ? names
        .map(name => assets.find(asset => asset && typeof asset === 'object' && asset.name === name))
        .find(Boolean)
    : null
  const digestMatch =
    archive && typeof archive.digest === 'string' ? /^sha256:([0-9a-f]{64})$/i.exec(archive.digest) : null

  if (
    !archive ||
    archive.state !== 'uploaded' ||
    typeof archive.browser_download_url !== 'string' ||
    !Number.isSafeInteger(archive.size) ||
    archive.size <= 0 ||
    archive.size > MAX_ARCHIVE_BYTES ||
    !digestMatch ||
    !SHA256_RE.test(digestMatch[1])
  ) {
    throw new Error(`release source asset metadata is invalid (${names.join(' or ')})`)
  }

  const archiveUrl = new URL(archive.browser_download_url, `${baseUrl}/`)

  if (archiveUrl.protocol !== 'http:' && archiveUrl.protocol !== 'https:') {
    throw new Error('release archive URL must use http(s)')
  }

  return {
    version,
    commit: value.target_commitish.toLowerCase(),
    channel: 'stable',
    archive: {
      url: archiveUrl.toString(),
      size: archive.size,
      sha256: digestMatch[1].toLowerCase()
    },
    ...(typeof value.published_at === 'string' ? { publishedAt: value.published_at } : {})
  }
}

function releaseIsNewer(release: AnsatzReleaseMetadata, currentVersion: string, currentCommit?: string | null): boolean {
  const parse = (version: string) => {
    const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+](.*))?$/.exec(version)

    return match ? [Number(match[1]), Number(match[2]), Number(match[3]), match[4] || ''] as const : null
  }

  const current = parse(currentVersion)
  const remote = parse(release.version)

  if (current && remote) {
    for (let index = 0; index < 3; index += 1) {
      if (remote[index] !== current[index]) {return remote[index] > current[index]}
    }

    if (remote[3] !== current[3]) {return !remote[3] && Boolean(current[3])}
  }

  return Boolean(currentCommit && release.commit !== currentCommit.toLowerCase())
}

/**
 * Resolve the version that represents the source currently used by a packaged
 * installation. The Electron bundle can intentionally lag behind the managed
 * backend source after a backend-only update, so the source marker is the
 * authoritative version when it is present.
 */
function resolveBundledCurrentVersion(appVersion: string, markerVersion?: string | null): string {
  const sourceVersion = typeof markerVersion === 'string' ? markerVersion.trim() : ''

  return sourceVersion || appVersion
}

async function fetchLatestRelease({
  environment = process.env,
  platform = process.platform,
  architecture = process.arch,
  fetchImpl = fetch
}: {
  environment?: NodeJS.ProcessEnv
  platform?: NodeJS.Platform
  architecture?: string
  fetchImpl?: typeof fetch
} = {}): Promise<AnsatzReleaseMetadata> {
  const baseUrl = resolveUpdateBaseUrl(environment)
  const endpoint = new URL(LATEST_RELEASE_PATH, `${baseUrl}/`)

  const response = await fetchImpl(endpoint, {
    headers: {
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    signal: AbortSignal.timeout(15_000)
  })

  if (!response.ok) {throw new Error(`release server returned HTTP ${response.status}`)}

  return validateReleaseMetadata(await response.json(), baseUrl, { platform, architecture })
}

export {
  DEFAULT_UPDATE_BASE_URL,
  fetchLatestRelease,
  LATEST_RELEASE_PATH,
  RELEASE_REPOSITORY,
  resolveBundledCurrentVersion,
  releaseIsNewer,
  resolveUpdateBaseUrl,
  SOURCE_ARCHIVE_ASSET_NAME,
  UPDATE_BASE_URL_ENV,
  validateReleaseMetadata
}
