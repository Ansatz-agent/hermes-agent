import crypto from 'node:crypto'
import os from 'node:os'
import path from 'node:path'

const ANSATZ_PRODUCT = Object.freeze({
  packageName: 'ansatz-voice-trace-client',
  productName: 'Ansatz',
  appId: 'cn.c2sml.ansatz.voice-trace-client',
  executableName: 'Ansatz',
  protocolScheme: 'ansatz-voice-trace',
  artifactPrefix: 'Ansatz',
  posixRuntimeDirectory: '.ansatz-voice-trace-client',
  windowsRuntimeDirectory: 'AnsatzVoiceTraceClient',
  runtimeHomeOverrideEnvironmentVariable: 'ANSATZ_VOICE_TRACE_CLIENT_HOME',
  // Bump the broker namespace when the auth wire contract changes.  The
  // detached Windows owner survives an app update, so keeping the old
  // namespace would reconnect to a pre-native-migration owner that silently
  // downgrades status/login requests to protocol v1.  That leaves users
  // authenticated locally but permanently classified as legacy, which blocks
  // standard-account Trace uploads.  The keyring service stays unchanged so
  // the new owner can safely migrate the existing credential in place.
  authRuntimeNamespace: 'ansatz-voice-trace-client-auth-v2',
  authKeyringService: 'cn.c2sml.ansatz.voice-trace-client.remote-auth',
  legacyAuthKeyringService: 'cn.c2sml.hermes.remote-auth',
  mediaProtocol: 'ansatz-media',
  embedSessionPartition: 'persist:ansatz-voice-trace-embed',
  previewSessionPartition: 'persist:ansatz-voice-trace-preview',
  remoteOauthSessionPartition: 'persist:ansatz-voice-trace-remote-oauth',
  linkTitleSession: 'ansatz:link-titles',
  desktopProduct: 'ansatz-voice-trace',
  canonicalCliLaunchers: Object.freeze([
    'ansatz',
    'ansatz-agent',
    'ansatz-acp'
  ] as const),
  legacyCliLaunchers: Object.freeze([
    'hermes',
    'hermes-agent',
    'hermes-acp'
  ] as const),
  posixLaunchers: Object.freeze([
    'ansatz-voice-trace',
    'ansatz-voice-trace-agent',
    'ansatz-voice-trace-acp'
  ] as const)
} as const)

function resolveAnsatzCliPath(
  canonicalPath: string,
  legacyPath: string,
  exists: (candidate: string) => boolean
) {
  return exists(canonicalPath) ? canonicalPath : legacyPath
}

function resolveAnsatzRuntimeRoot(platform, homeDirectory, localAppData) {
  if (platform === 'win32') {
    if (!String(localAppData || '').trim()) {
      throw new Error('LOCALAPPDATA is required to resolve the Windows runtime root.')
    }

    return path.win32.join(String(localAppData), ANSATZ_PRODUCT.windowsRuntimeDirectory)
  }

  if (!String(homeDirectory || '').trim()) {
    throw new Error('The user home directory is required to resolve the runtime root.')
  }

  return path.posix.join(String(homeDirectory), ANSATZ_PRODUCT.posixRuntimeDirectory)
}

interface AnsatzDesktopRuntimeRootOptions {
  isPackaged: boolean
  platform: NodeJS.Platform
  homeDirectory: string
  localAppData?: string
  inheritedHermesHome?: string
  dedicatedRuntimeHomeOverride?: string
  desktopUserDataOverride?: string
  normalizeInheritedRoot?: (root: string) => string
}

function resolveAnsatzDesktopRuntimeRoot({
  isPackaged,
  platform,
  homeDirectory,
  localAppData,
  inheritedHermesHome,
  dedicatedRuntimeHomeOverride,
  desktopUserDataOverride,
  normalizeInheritedRoot = root => root
}: AnsatzDesktopRuntimeRootOptions) {
  const pathApi = platform === 'win32' ? path.win32 : path.posix

  if (isPackaged && dedicatedRuntimeHomeOverride) {
    return pathApi.resolve(dedicatedRuntimeHomeOverride)
  }

  // Keep the generic engine override available to source-checkout developers,
  // but never let ambient shell state redirect an installed product back into
  // a legacy Hermes runtime root.
  if (!isPackaged && inheritedHermesHome) {
    return normalizeInheritedRoot(inheritedHermesHome)
  }

  if (desktopUserDataOverride) {
    return pathApi.join(pathApi.resolve(desktopUserDataOverride), 'ansatz-voice-trace-home')
  }

  return resolveAnsatzRuntimeRoot(platform, homeDirectory, localAppData)
}

function ansatzAuthEnvironment(
  hermesHome: string,
  source: Record<string, string | undefined> = process.env
): Record<string, string | undefined> {
  const environment = { ...source }

  // Reading a legacy macOS Keychain item from a newly signed/repacked app can
  // trigger an unexpected system-password prompt. Keep the legacy service as
  // an explicit compatibility identifier, but never opt local Desktop startup
  // into migration merely because the parent environment contains it.
  delete environment.HERMES_AUTH_LEGACY_KEYRING_SERVICE

  return {
    ...environment,
    HERMES_HOME: hermesHome,
    HERMES_AUTH_RUNTIME_NAMESPACE: ANSATZ_PRODUCT.authRuntimeNamespace,
    HERMES_AUTH_KEYRING_SERVICE: ANSATZ_PRODUCT.authKeyringService
  }
}

function buildBundledRuntimeValidationEnvironment(
  activeRoot: string,
  hermesHome: string,
  source: Record<string, string | undefined> = process.env
) {
  return {
    ...ansatzAuthEnvironment(hermesHome, source),
    PYTHONPATH: [activeRoot, source.PYTHONPATH].filter(Boolean).join(path.delimiter)
  }
}

function buildAnsatzTerminalEnvironment(
  hermesHome: string,
  appVersion: string,
  source: Record<string, string | undefined> = process.env
) {
  const environment = ansatzAuthEnvironment(hermesHome, source)

  // Electron is commonly launched through `npm run dev`; do not leak npm's
  // managed prefix into a user's interactive shell (nvm/proto warn loudly).
  for (const key of Object.keys(environment)) {
    if (key === 'npm_config_prefix' || key.startsWith('npm_config_') || key.startsWith('npm_package_')) {
      delete environment[key]
    }
  }

  // The PTY is a real xterm-compatible terminal. Ambient color-detection
  // values from the Electron launcher must not misclassify it.
  delete environment.NO_COLOR
  delete environment.FORCE_COLOR
  delete environment.COLORFGBG

  environment.COLORTERM = 'truecolor'
  environment.HERMES_DESKTOP_TERMINAL = '1'
  environment.LC_CTYPE = environment.LC_CTYPE || 'UTF-8'
  environment.TERM = 'xterm-256color'
  environment.TERM_PROGRAM = ANSATZ_PRODUCT.executableName
  environment.TERM_PROGRAM_VERSION = appVersion

  return environment
}

function resolveAnsatzSshControlDirectory(hermesHome: string) {
  const runtimeDirectory = path.join(hermesHome, 'desktop-ssh')

  if (process.platform === 'win32') {
    return runtimeDirectory
  }

  // OpenSSH creates a temporary listener at `<ControlPath>.<16 chars>`.
  // macOS' 104-byte sun_path budget includes that suffix, not just the final
  // socket name, and Unicode path segments consume their encoded byte length.
  const worstCaseSocketPath = path.posix.join(runtimeDirectory, `${'0'.repeat(16)}.sock`) + `.${'x'.repeat(16)}`

  if (Buffer.byteLength(worstCaseSocketPath) < 104) {
    return runtimeDirectory
  }

  const userIdentity =
    typeof process.getuid === 'function'
      ? String(process.getuid())
      : crypto.createHash('sha256').update(os.homedir()).digest('hex').slice(0, 8)

  const runtimeIdentity = crypto
    .createHash('sha256')
    .update(path.posix.resolve(hermesHome))
    .digest('hex')
    .slice(0, 16)

  return path.posix.join('/tmp', `ansatz-vtc-ssh-${userIdentity}-${runtimeIdentity}`)
}

export {
  ANSATZ_PRODUCT,
  ansatzAuthEnvironment,
  buildAnsatzTerminalEnvironment,
  buildBundledRuntimeValidationEnvironment,
  resolveAnsatzCliPath,
  resolveAnsatzDesktopRuntimeRoot,
  resolveAnsatzRuntimeRoot,
  resolveAnsatzSshControlDirectory
}
