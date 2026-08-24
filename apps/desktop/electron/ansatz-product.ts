import path from 'node:path'

const ANSATZ_PRODUCT = Object.freeze({
  packageName: 'ansatz-voice-trace-client',
  productName: 'Ansatz',
  appId: 'cn.c2sml.ansatz.voice-trace-client',
  executableName: 'Ansatz',
  protocolScheme: 'ansatz-voice-trace',
  artifactPrefix: 'Ansatz',
  legacyUserDataDirectory: 'Ansatz Voice Trace Client',
  posixRuntimeDirectory: '.ansatz-voice-trace-client',
  windowsRuntimeDirectory: 'AnsatzVoiceTraceClient'
} as const)

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

function resolveAnsatzUserDataRoot(platform, appDataDirectory) {
  if (!String(appDataDirectory || '').trim()) {
    throw new Error('The Electron app-data directory is required to resolve user data.')
  }

  const platformPath = platform === 'win32' ? path.win32 : path.posix
  return platformPath.join(String(appDataDirectory), ANSATZ_PRODUCT.legacyUserDataDirectory)
}

export { ANSATZ_PRODUCT, resolveAnsatzRuntimeRoot, resolveAnsatzUserDataRoot }
