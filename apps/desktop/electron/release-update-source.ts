const UPDATE_BASE_URL_ENV = 'ANSATZ_UPDATE_BASE_URL'
const DEFAULT_UPDATE_BASE_URL = 'https://setup.hermes-agent.nousresearch.com'
const LATEST_RELEASE_PATH = '/api/v1/ansatz/releases/latest'
const COMMIT_RE = /^[0-9a-f]{40}$/i
const SHA256_RE = /^[0-9a-f]{64}$/

interface ReleaseArchive {
  url: string
  size: number
  sha256: string
}

export interface AnsatzReleaseMetadata {
  schemaVersion: 1
  product: 'ansatz' | 'ansatz-agent'
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

function validateReleaseMetadata(raw: unknown, baseUrl: string): AnsatzReleaseMetadata {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('release metadata must be a JSON object')
  }

  const value = raw as Record<string, any>

  if (value.schemaVersion !== 1) {throw new Error('release metadata schemaVersion must be 1')}

  if (value.product !== 'ansatz' && value.product !== 'ansatz-agent') {
    throw new Error('release metadata product must identify Ansatz')
  }

  if (typeof value.version !== 'string' || !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(value.version)) {
    throw new Error('release metadata version is invalid')
  }

  if (typeof value.commit !== 'string' || !COMMIT_RE.test(value.commit) || /^0+$/.test(value.commit)) {
    throw new Error('release metadata commit must be a real Git SHA')
  }

  if (
    !value.archive ||
    typeof value.archive.url !== 'string' ||
    !Number.isSafeInteger(value.archive.size) ||
    value.archive.size <= 0 ||
    typeof value.archive.sha256 !== 'string' ||
    !SHA256_RE.test(value.archive.sha256)
  ) {
    throw new Error('release archive metadata is invalid')
  }

  const archiveUrl = new URL(value.archive.url, `${baseUrl}/`)

  if (archiveUrl.protocol !== 'http:' && archiveUrl.protocol !== 'https:') {
    throw new Error('release archive URL must use http(s)')
  }

  return {
    schemaVersion: 1,
    product: value.product,
    version: value.version,
    commit: value.commit.toLowerCase(),
    channel: typeof value.channel === 'string' && value.channel ? value.channel : 'stable',
    archive: {
      url: archiveUrl.toString(),
      size: value.archive.size,
      sha256: value.archive.sha256.toLowerCase()
    },
    ...(typeof value.publishedAt === 'string' ? { publishedAt: value.publishedAt } : {})
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
  endpoint.searchParams.set('channel', 'stable')
  endpoint.searchParams.set('platform', platformName(platform))
  endpoint.searchParams.set('arch', architectureName(architecture))

  const response = await fetchImpl(endpoint, {
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(15_000)
  })

  if (!response.ok) {throw new Error(`release server returned HTTP ${response.status}`)}

  return validateReleaseMetadata(await response.json(), baseUrl)
}

export {
  DEFAULT_UPDATE_BASE_URL,
  fetchLatestRelease,
  LATEST_RELEASE_PATH,
  releaseIsNewer,
  resolveUpdateBaseUrl,
  UPDATE_BASE_URL_ENV,
  validateReleaseMetadata
}
