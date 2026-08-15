import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const PLAYWRIGHT_DOWNLOAD_PATTERNS = [
  /\bDownloading\s+(?:Chromium|Chrome for Testing|FFMPEG)\b/i,
  /\bplaywright(?:@\S+)?\s+install(?:\s+--\S+)*\s+chromium\b/i
]

export function forbiddenBrowserDownloadLine(logText) {
  for (const line of String(logText).split(/\r?\n/)) {
    if (PLAYWRIGHT_DOWNLOAD_PATTERNS.some(pattern => pattern.test(line))) {
      return line.trim()
    }
  }
  return null
}

export function findNewestDmg(releaseDir) {
  const absoluteReleaseDir = path.resolve(releaseDir)
  const candidates = fs.existsSync(absoluteReleaseDir)
    ? fs.readdirSync(absoluteReleaseDir)
      .filter(name => /^Hermes-.+-mac-arm64\.dmg$/.test(name))
      .map(name => path.join(absoluteReleaseDir, name))
      .filter(filePath => fs.statSync(filePath).isFile())
      .sort((left, right) => {
        const mtimeDelta = fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs
        return mtimeDelta || right.localeCompare(left)
      })
    : []

  if (candidates.length === 0) {
    throw new Error(`no macOS arm64 Hermes DMG found in ${absoluteReleaseDir}`)
  }
  return candidates[0]
}

function usage() {
  return [
    'Usage:',
    '  node scripts/desktop-dmg-contract.mjs validate-log <log-file>',
    '  node scripts/desktop-dmg-contract.mjs find-dmg <release-directory>'
  ].join('\n')
}

function main(argv) {
  const [command, target, ...extra] = argv
  if (!command || !target || extra.length > 0) {
    throw new Error(usage())
  }

  if (command === 'validate-log') {
    const absoluteLogPath = path.resolve(target)
    const matchedLine = forbiddenBrowserDownloadLine(fs.readFileSync(absoluteLogPath, 'utf8'))
    if (matchedLine) {
      throw new Error(`Playwright browser download detected: ${matchedLine}`)
    }
    process.stdout.write(`No Playwright browser download detected in ${absoluteLogPath}\n`)
    return
  }

  if (command === 'find-dmg') {
    process.stdout.write(`${findNewestDmg(target)}\n`)
    return
  }

  throw new Error(usage())
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : ''
if (invokedPath === import.meta.url) {
  try {
    main(process.argv.slice(2))
  } catch (error) {
    process.stderr.write(`Hermes DMG build: ${error.message}\n`)
    process.exitCode = 1
  }
}
