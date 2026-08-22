export function exeIdentityOptions({ icon, productVersion }) {
  if (!productVersion || typeof productVersion !== 'string') {
    throw new Error('product version is required to stamp Hermes.exe')
  }
  return {
    icon,
    'file-version': productVersion,
    'product-version': productVersion,
    'version-string': {
      ProductName: 'Hermes',
      FileDescription: 'Hermes',
      CompanyName: 'Nous Research',
      LegalCopyright: 'Copyright (c) 2026 Nous Research'
    }
  }
}
