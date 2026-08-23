export function exeIdentityOptions({ icon, productVersion }) {
  if (!productVersion || typeof productVersion !== 'string') {
    throw new Error('product version is required to stamp AnsatzVoiceTraceClient.exe')
  }
  return {
    icon,
    'file-version': productVersion,
    'product-version': productVersion,
    'version-string': {
      ProductName: 'Ansatz Voice Trace Client',
      FileDescription: 'Ansatz Voice Trace Client',
      CompanyName: 'Ansatz Agent',
      LegalCopyright: 'Copyright (c) 2026 Ansatz Agent'
    }
  }
}
