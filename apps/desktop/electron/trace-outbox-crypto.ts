import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { promisify } from 'node:util'
import { brotliCompress, brotliDecompress } from 'node:zlib'

const brotliCompressAsync = promisify(brotliCompress)
const brotliDecompressAsync = promisify(brotliDecompress)

const AES_256_KEY_BYTES = 32
const GCM_NONCE_BYTES = 12
const GCM_TAG_BYTES = 16

export interface EncryptedTraceRecord {
  nonce: Buffer
  ciphertext: Buffer
  tag: Buffer
  originalBytes: number
}

export interface TraceKeyProtector {
  available(): boolean
  wrap(key: Buffer): Buffer
  unwrap(wrappedKey: Buffer): Buffer
}

export interface SafeStorageKeyApi {
  isEncryptionAvailable(): boolean
  encryptString(plaintext: string): Buffer
  decryptString(ciphertext: Buffer): string
}

function requireDataKey(key: Buffer): void {
  if (!Buffer.isBuffer(key) || key.length !== AES_256_KEY_BYTES) {
    throw new TypeError('invalid_encryption_key')
  }
}

export function createSafeStorageTraceKeyProtector(safeStorage: SafeStorageKeyApi): TraceKeyProtector {
  const available = (): boolean => {
    try {
      return safeStorage.isEncryptionAvailable() === true
    } catch {
      return false
    }
  }

  const requireAvailable = (): void => {
    if (!available()) {
      throw new Error('secure_key_storage_unavailable')
    }
  }

  return {
    available,
    wrap(key: Buffer): Buffer {
      requireAvailable()
      requireDataKey(key)

      return Buffer.from(safeStorage.encryptString(key.toString('base64')))
    },
    unwrap(wrappedKey: Buffer): Buffer {
      requireAvailable()

      if (!Buffer.isBuffer(wrappedKey) || wrappedKey.length === 0) {
        throw new TypeError('invalid_wrapped_key')
      }

      const encoded = safeStorage.decryptString(wrappedKey)

      if (!/^[A-Za-z0-9+/]+={0,2}$/.test(encoded) || encoded.length % 4 !== 0) {
        throw new TypeError('invalid_wrapped_key')
      }

      const key = Buffer.from(encoded, 'base64')

      requireDataKey(key)

      return key
    }
  }
}

export async function encryptTraceRecord(plaintext: Buffer, key: Buffer, aad: Buffer): Promise<EncryptedTraceRecord> {
  requireDataKey(key)

  const compressed = await brotliCompressAsync(plaintext)
  const nonce = randomBytes(GCM_NONCE_BYTES)
  const cipher = createCipheriv('aes-256-gcm', key, nonce)

  cipher.setAAD(aad)
  const ciphertext = Buffer.concat([cipher.update(compressed), cipher.final()])

  return {
    nonce,
    ciphertext,
    tag: cipher.getAuthTag(),
    originalBytes: plaintext.length
  }
}

export async function decryptTraceRecord(record: EncryptedTraceRecord, key: Buffer, aad: Buffer): Promise<Buffer> {
  requireDataKey(key)

  if (!Buffer.isBuffer(record.nonce) || record.nonce.length !== GCM_NONCE_BYTES) {
    throw new TypeError('invalid_nonce')
  }

  if (!Buffer.isBuffer(record.tag) || record.tag.length !== GCM_TAG_BYTES) {
    throw new TypeError('invalid_auth_tag')
  }

  const decipher = createDecipheriv('aes-256-gcm', key, record.nonce)

  decipher.setAAD(aad)
  decipher.setAuthTag(record.tag)
  const compressed = Buffer.concat([decipher.update(record.ciphertext), decipher.final()])
  const plaintext = await brotliDecompressAsync(compressed)

  if (plaintext.length !== record.originalBytes) {
    throw new Error('invalid_original_length')
  }

  return plaintext
}
