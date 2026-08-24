export function exeIdentityOptions({ icon, productVersion }) {
  if (!productVersion || typeof productVersion !== 'string') {
    throw new Error('product version is required to stamp Ansatz.exe')
  }
  return {
    icon,
    'file-version': productVersion,
    'product-version': productVersion,
    'version-string': {
      ProductName: 'Ansatz',
      FileDescription: 'Ansatz',
      CompanyName: 'Ansatz Agent',
      LegalCopyright: 'Copyright (c) 2026 Ansatz Agent'
    }
  }
}
