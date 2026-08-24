import managedOrigins from '../../../docs/security/hermes-managed-download-origins.json' with { type: 'json' }

export const MANAGED_DOWNLOAD_PHASES = [
  'auth-payload-build',
  'runtime-install',
  'repair',
  'update',
  'lazy-feature'
] as const

export type ManagedDownloadPhase = (typeof MANAGED_DOWNLOAD_PHASES)[number]

type ManagedOrigin = {
  id: string
  domestic_primary?: string | null
  domestic_secondary?: string | null
  official_fallback?: string | null
}

const SAFE_ENV_KEYS = Object.freeze([
  'APPDATA',
  'DBUS_SESSION_BUS_ADDRESS',
  'DISPLAY',
  'HOME',
  'LANG',
  'LANGUAGE',
  'LC_ALL',
  'LC_CTYPE',
  'LOCALAPPDATA',
  'LOGNAME',
  'PATH',
  'PATHEXT',
  'SHELL',
  'SYSTEMROOT',
  'TEMP',
  'TMP',
  'TMPDIR',
  'USER',
  'USERPROFILE',
  'WAYLAND_DISPLAY',
  'WINDIR',
  'XDG_CACHE_HOME',
  'XDG_CONFIG_HOME',
  'XDG_DATA_HOME',
  'XDG_RUNTIME_DIR'
])

const origins = new Map(
  (managedOrigins.entries as ManagedOrigin[]).map(entry => [entry.id, Object.freeze({ ...entry })])
)

function requiredOrigin(id: string): Readonly<ManagedOrigin> {
  const entry = origins.get(id)

  if (!entry?.domestic_primary) {
    throw new Error(`managed download origin ${id} has no domestic primary`)
  }

  return entry
}

function primary(id: string): string {
  return requiredOrigin(id).domestic_primary as string
}

function secondary(id: string): string {
  const entry = requiredOrigin(id)

  if (!entry.domestic_secondary) {
    throw new Error(`managed download origin ${id} has no domestic secondary`)
  }

  return entry.domestic_secondary
}

export const MANAGED_DOMESTIC_DOWNLOADS = Object.freeze({
  electronBuilder: primary('electron-builder-binaries'),
  electronPrimary: primary('electron-runtime'),
  electronSecondary: secondary('electron-runtime'),
  nodePrimary: primary('node-runtime'),
  nodeSecondary: secondary('node-runtime'),
  npmPrimary: primary('npm-packages'),
  playwrightPrimary: primary('playwright-browser'),
  playwrightSecondary: secondary('playwright-browser'),
  pythonPrimary: primary('python-packages'),
  pythonSecondary: secondary('python-packages')
})

/**
 * Build a fail-closed environment for every product-managed child process.
 *
 * This intentionally does not spread `source`: package-manager config, proxy
 * redirects, Python injection and Hugging Face credentials are never inherited
 * into pre-auth, repair, update or lazy-feature subprocesses.
 */
export function buildManagedDownloadEnvironment(
  source: NodeJS.ProcessEnv = process.env,
  phase: ManagedDownloadPhase
): NodeJS.ProcessEnv {
  if (!MANAGED_DOWNLOAD_PHASES.includes(phase)) {
    throw new Error(`unknown managed download phase: ${String(phase)}`)
  }

  const env: NodeJS.ProcessEnv = {}

  for (const key of SAFE_ENV_KEYS) {
    const value = source[key]

    if (typeof value === 'string') {
      env[key] = value
    }
  }

  const nullConfig = typeof source.SYSTEMROOT === 'string' || typeof source.WINDIR === 'string' ? 'NUL' : '/dev/null'

  env.UV_NO_CONFIG = '1'
  env.UV_DEFAULT_INDEX = MANAGED_DOMESTIC_DOWNLOADS.pythonPrimary
  env.UV_INDEX = MANAGED_DOMESTIC_DOWNLOADS.pythonPrimary
  env.PIP_CONFIG_FILE = nullConfig
  env.PIP_INDEX_URL = MANAGED_DOMESTIC_DOWNLOADS.pythonPrimary
  env.PIP_DISABLE_PIP_VERSION_CHECK = '1'
  env.PIP_NO_INPUT = '1'
  env.HERMES_UV_FALLBACK_INDEX = MANAGED_DOMESTIC_DOWNLOADS.pythonSecondary

  env.NPM_CONFIG_REGISTRY = MANAGED_DOMESTIC_DOWNLOADS.npmPrimary
  env.npm_config_registry = MANAGED_DOMESTIC_DOWNLOADS.npmPrimary
  env.NPM_CONFIG_FUND = 'false'
  env.NPM_CONFIG_AUDIT = 'false'

  env.NODEJS_ORG_MIRROR = MANAGED_DOMESTIC_DOWNLOADS.nodePrimary
  env.HERMES_NODE_MIRROR = MANAGED_DOMESTIC_DOWNLOADS.nodePrimary
  env.HERMES_NODE_FALLBACK_MIRROR = MANAGED_DOMESTIC_DOWNLOADS.nodeSecondary
  env.ELECTRON_MIRROR = MANAGED_DOMESTIC_DOWNLOADS.electronPrimary
  env.HERMES_ELECTRON_FALLBACK_MIRROR = MANAGED_DOMESTIC_DOWNLOADS.electronSecondary
  env.ELECTRON_BUILDER_BINARIES_MIRROR = MANAGED_DOMESTIC_DOWNLOADS.electronBuilder
  env.PLAYWRIGHT_DOWNLOAD_HOST = MANAGED_DOMESTIC_DOWNLOADS.playwrightPrimary
  env.HERMES_PLAYWRIGHT_FALLBACK_MIRROR = MANAGED_DOMESTIC_DOWNLOADS.playwrightSecondary

  // Product-managed model sources are bundled or explicitly registered. Do
  // not silently fall through to Hugging Face or inherit a user's access token.
  env.HF_HUB_OFFLINE = '1'
  env.HF_HUB_DISABLE_TELEMETRY = '1'
  env.CI = '1'

  return env
}
