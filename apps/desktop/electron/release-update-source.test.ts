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
    tag_name: 'v0.18.0',
    target_commitish: COMMIT,
    draft: false,
    prerelease: false,
    published_at: '2026-09-01T00:00:00Z',
    assets: [
      {
        name: 'hermes-backend.tar.gz',
        state: 'uploaded',
        size: 123,
        digest: `sha256:${SHA256}`,
        browser_download_url: '/Ansatz-agent/hermes-agent/releases/download/v0.18.0/hermes-backend.tar.gz'
      }
    ],
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
    expect(release.archive.url).toBe(
      'http://192.168.56.1:8765/Ansatz-agent/hermes-agent/releases/download/v0.18.0/hermes-backend.tar.gz'
    )
  })

  it('queries the GitHub-compatible latest-release endpoint', async () => {
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
    expect(requested.pathname).toBe('/repos/Ansatz-agent/hermes-agent/releases/latest')
    expect(requested.search).toBe('')
    expect(release.version).toBe('0.18.0')
  })

  it('treats a different commit at the same version as a refreshed release', () => {
    const release = validateReleaseMetadata(metadata({ tag_name: 'v0.17.0' }), 'https://updates.example')
    expect(releaseIsNewer(release, '0.17.0', 'c'.repeat(40))).toBe(true)
    expect(releaseIsNewer(release, '0.18.0', 'c'.repeat(40))).toBe(false)
  })
})
