import { describe, expect, it } from 'vitest'

import {
  fetchLatestRelease,
  releaseIsNewer,
  resolveUpdateBaseUrl,
  validateReleaseMetadata
} from './release-update-source'

const COMMIT = 'a'.repeat(40)
const SHA256 = 'b'.repeat(64)

function metadata(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    product: 'ansatz',
    channel: 'stable',
    version: '0.18.0',
    commit: COMMIT,
    archive: {
      url: '/static/ansatz-0.18.0.tar.gz',
      size: 123,
      sha256: SHA256
    },
    ...overrides
  }
}

describe('release update source', () => {
  it('accepts a plain HTTP VM host override', () => {
    expect(resolveUpdateBaseUrl({ ANSATZ_UPDATE_BASE_URL: 'http://192.168.56.1:8765/' })).toBe(
      'http://192.168.56.1:8765'
    )
  })

  it('resolves static archive URLs against the configured server', () => {
    const release = validateReleaseMetadata(metadata(), 'http://192.168.56.1:8765')
    expect(release.archive.url).toBe('http://192.168.56.1:8765/static/ansatz-0.18.0.tar.gz')
  })

  it('queries the platform-specific latest-release endpoint', async () => {
    let requestedUrl = ''

    const fetchImpl: typeof fetch = async input => {
      requestedUrl = String(input)

      return new Response(JSON.stringify(metadata()), { status: 200 })
    }

    const release = await fetchLatestRelease({
      environment: { ANSATZ_UPDATE_BASE_URL: 'http://10.0.2.2:9000' },
      platform: 'win32',
      architecture: 'x64',
      fetchImpl
    })

    const requested = new URL(requestedUrl)
    expect(requested.pathname).toBe('/api/v1/ansatz/releases/latest')
    expect(requested.searchParams.get('platform')).toBe('windows')
    expect(requested.searchParams.get('arch')).toBe('x64')
    expect(release.version).toBe('0.18.0')
  })

  it('treats a different commit at the same version as a refreshed release', () => {
    const release = validateReleaseMetadata(metadata({ version: '0.17.0' }), 'https://updates.example')
    expect(releaseIsNewer(release, '0.17.0', 'c'.repeat(40))).toBe(true)
    expect(releaseIsNewer(release, '0.18.0', 'c'.repeat(40))).toBe(false)
  })
})
