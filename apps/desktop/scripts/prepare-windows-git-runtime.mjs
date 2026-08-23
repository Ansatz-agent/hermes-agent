/**
 * Turn the pinned Git for Windows PortableGit release into the smaller
 * Git/Bash runtime shipped by Hermes Desktop.
 *
 * The upstream self-extracting archive is build input only. We verify its
 * official digest, remove documentation, tests, and GUI-only Git tools, then
 * package the remaining runtime as an XZ-compressed tar that Windows 10/11
 * can extract with its built-in bsdtar.
 */

import { execFileSync } from "node:child_process"
import { createHash } from "node:crypto"
import {
  chmodSync,
  copyFileSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync
} from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, relative, resolve, sep } from "node:path"

import { isMain } from "./utils.mjs"

export const WINDOWS_PORTABLE_GIT_RELEASE_FILE = "PortableGit-2.55.0.3-64-bit.7z.exe"
export const WINDOWS_PORTABLE_GIT_RELEASE_SHA256 =
  "ab00566336b5472120f9a52d34f2e79c5406535792acb0548001ffd0bd090e5d"
export const WINDOWS_GIT_RUNTIME_FILE = "git-bash-runtime.tar.xz"
export const WINDOWS_GIT_RUNTIME_PROVENANCE_FILE = "git-bash-runtime.provenance.json"

const DESKTOP_ROOT = resolve(import.meta.dirname, "..")
const WINDOWS_PREREQS_ROOT = join(DESKTOP_ROOT, "build", "windows-prereqs")
const DEFAULT_SOURCE_PATH = join(WINDOWS_PREREQS_ROOT, "portable-git-source.7z.exe")
const DEFAULT_OUTPUT_PATH = join(WINDOWS_PREREQS_ROOT, WINDOWS_GIT_RUNTIME_FILE)
const DEFAULT_PROVENANCE_PATH = join(WINDOWS_PREREQS_ROOT, WINDOWS_GIT_RUNTIME_PROVENANCE_FILE)
const SHA256_RE = /^[0-9a-f]{64}$/
const REQUIRED_RUNTIME_ENTRIES = Object.freeze([
  "bin/bash.exe",
  "cmd/git.exe",
  "usr/bin/cat.exe",
  "usr/bin/sh.exe"
])

const GUI_ONLY_PREFIXES = Object.freeze([
  "cmd/git-gui.exe",
  "cmd/gitk.exe",
  "mingw64/bin/git-gui.exe",
  "mingw64/bin/gitk.exe",
  "mingw64/libexec/git-core/git-citool",
  "mingw64/libexec/git-core/git-gui",
  "mingw64/share/git-gui/",
  "mingw64/share/gitk/",
  "mingw64/share/gitweb/"
])

function sha256File(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex")
}

function normalizeEntry(entry) {
  return entry.replaceAll("\\", "/").replace(/^\.\//, "").replace(/\/$/, "")
}

function isLegalNotice(entry) {
  const normalized = normalizeEntry(entry)
  const name = normalized.split("/").at(-1) || ""
  return /^(license|licence|copying|notice)(\.[a-z0-9._-]+)?$/i.test(name)
}

export function gitRuntimeEntryIsForbidden(entry, { directory = false } = {}) {
  const normalized = normalizeEntry(entry)
  if (!normalized) return false

  const parts = normalized.split("/")
  if (
    normalized.startsWith("/") ||
    parts.includes("..") ||
    entry.includes("\\")
  ) {
    return true
  }

  const lower = normalized.toLowerCase()
  const lowerParts = lower.split("/")
  if (
    lowerParts.some(
      part =>
        part === "test" ||
        part === "tests" ||
        part === "test2" ||
        part === "__tests__" ||
        part === "tutor" ||
        part === "manual" ||
        part === "manuals" ||
        part === "example" ||
        part === "examples" ||
        part === "demo" ||
        part === "demos"
    )
  ) {
    return true
  }
  if (
    lowerParts.some(
      part =>
        part === "doc" ||
        part === "docs" ||
        part === "website" ||
        part === "gtk-doc" ||
        part.endsWith("-doc")
    )
  ) {
    return true
  }
  if (
    lower.includes("/share/man/") ||
    lower.includes("/share/info/") ||
    GUI_ONLY_PREFIXES.some(prefix => lower === prefix || lower.startsWith(prefix))
  ) {
    return true
  }
  if (directory) return false
  if (isLegalNotice(normalized)) return false
  if (
    /\.(html?|pdf|md|rst|adoc|pod|gguf|onnx|safetensors|tflite|pt|pth|[1-9])$/i.test(lower) ||
    /(^|\/)(readme|authors|changelog|news|thanks)(\.[^/]*)?$/i.test(lower)
  ) {
    return true
  }
  if (/(^|\/)[^/]+\.(test|spec)\.[^/]+$/i.test(lower)) return true

  return false
}

function assertRegularFile(filePath, label) {
  const stats = lstatSync(filePath)
  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link file: ${filePath}`)
  }
  return stats
}

function pruneRuntimeTree(root) {
  function visit(directory) {
    for (const item of readdirSync(directory, { withFileTypes: true })) {
      const absolute = join(directory, item.name)
      const entry = relative(root, absolute).split(sep).join("/")
      if (item.isSymbolicLink()) {
        rmSync(absolute, { force: true })
      } else if (item.isDirectory()) {
        visit(absolute)
        if (readdirSync(absolute).length === 0) rmSync(absolute, { recursive: true, force: true })
      } else if (item.isFile() && isLegalNotice(entry) && gitRuntimeEntryIsForbidden(entry)) {
        const parts = entry.split("/")
        const noticeName = parts.pop()
        const noticeParent = parts.join("__").replace(/[^a-z0-9._-]+/gi, "_") || "root"
        const noticeDirectory = join(root, "third-party-licenses", noticeParent)
        mkdirSync(noticeDirectory, { recursive: true })
        renameSync(absolute, join(noticeDirectory, noticeName))
      } else if (!item.isFile() || gitRuntimeEntryIsForbidden(entry)) {
        rmSync(absolute, { force: true })
      }
    }
  }

  visit(root)
}

export function materializeRuntimeHardLinks(root) {
  let temporaryIndex = 0

  function visit(directory) {
    for (const item of readdirSync(directory, { withFileTypes: true })) {
      const absolute = join(directory, item.name)
      if (item.isDirectory()) {
        visit(absolute)
        continue
      }
      if (!item.isFile()) {
        throw new Error(`Git Bash runtime contains an unsupported entry before archiving: ${absolute}`)
      }

      const stats = lstatSync(absolute)
      if (stats.nlink <= 1) continue

      temporaryIndex += 1
      const materialized = `${absolute}.hermes-materialized-${process.pid}-${temporaryIndex}`
      const originalLink = `${absolute}.hermes-hardlink-${process.pid}-${temporaryIndex}`
      try {
        copyFileSync(absolute, materialized)
        chmodSync(materialized, stats.mode)
        utimesSync(materialized, stats.atime, stats.mtime)
        renameSync(absolute, originalLink)
        try {
          renameSync(materialized, absolute)
        } catch (error) {
          renameSync(originalLink, absolute)
          throw error
        }
        rmSync(originalLink, { force: true })
      } finally {
        rmSync(materialized, { force: true })
        rmSync(originalLink, { force: true })
      }
    }
  }

  visit(root)
}

function listRuntimeEntries(archivePath) {
  const command = process.platform === "win32" ? "tar.exe" : "bsdtar"
  const args = ["-tf", archivePath]
  return execFileSync(command, args, { encoding: "utf8" })
    .split(/\r?\n/)
    .filter(Boolean)
    .map(raw => ({ path: normalizeEntry(raw), directory: /[\\/]$/.test(raw) }))
    .filter(entry => entry.path)
}

export function auditGitRuntimeArchive(archivePath) {
  assertRegularFile(archivePath, "Git Bash runtime archive")
  const tarCommand = process.platform === "win32" ? "tar.exe" : "bsdtar"
  const unsafeType = execFileSync(tarCommand, ["-tvf", archivePath], { encoding: "utf8" })
    .split(/\r?\n/)
    .find(line => line && line[0] !== "-" && line[0] !== "d")
  if (unsafeType) {
    throw new Error(`Git Bash runtime archive contains a link or unsupported entry type: ${unsafeType}`)
  }
  const entries = listRuntimeEntries(archivePath)
  const entrySet = new Set(entries.map(entry => entry.path))
  for (const required of REQUIRED_RUNTIME_ENTRIES) {
    if (!entrySet.has(required)) {
      throw new Error(`Git Bash runtime archive is missing required entry ${required}`)
    }
  }
  const forbidden = entries.find(entry =>
    gitRuntimeEntryIsForbidden(entry.path, { directory: entry.directory })
  )
  if (forbidden) {
    throw new Error(
      `Git Bash runtime archive contains forbidden documentation, test, or GUI entry ${forbidden.path}`
    )
  }
  return entries.map(entry => entry.path)
}

async function extractPortableGit(sourcePath, destination) {
  if (process.platform === "win32") {
    execFileSync(sourcePath, [`-o${destination}`, "-y"], { stdio: "inherit", windowsHide: true })
  } else {
    // libarchive on older macOS versions cannot decode the current
    // PortableGit LZMA settings. Reuse electron-builder's pinned 7zip toolset
    // for developer-side cross-package verification.
    const { getPath7za } = await import("app-builder-lib/out/toolsets/7zip.js")
    const sevenZip = await getPath7za()
    execFileSync(sevenZip, ["x", sourcePath, `-o${destination}`, "-y"], { stdio: "inherit" })
  }
}

function createRuntimeArchive(sourceRoot, outputPath) {
  rmSync(outputPath, { force: true })
  const tarCommand = process.platform === "win32" ? "tar.exe" : "bsdtar"
  const temporaryTar = `${outputPath}.tmp-${process.pid}.tar`
  const compressedTar = `${temporaryTar}.xz`
  try {
    execFileSync(tarCommand, ["-cf", temporaryTar, "-C", sourceRoot, "."], { stdio: "inherit" })
    const xzCommand =
      process.platform === "win32" ? join(sourceRoot, "mingw64", "bin", "xz.exe") : "xz"
    assertRegularFile(
      process.platform === "win32" ? xzCommand : join(sourceRoot, "mingw64", "bin", "xz.exe"),
      "PortableGit xz runtime"
    )
    execFileSync(
      xzCommand,
      ["--threads=0", "--x86", "--lzma2=preset=9e", "--force", temporaryTar],
      { stdio: "inherit" }
    )
    renameSync(compressedTar, outputPath)
  } finally {
    rmSync(temporaryTar, { force: true })
    rmSync(compressedTar, { force: true })
  }
}

export async function prepareWindowsGitRuntime({
  sourcePath = DEFAULT_SOURCE_PATH,
  outputPath = DEFAULT_OUTPUT_PATH,
  provenancePath = DEFAULT_PROVENANCE_PATH,
  expectedSourceSha256 = WINDOWS_PORTABLE_GIT_RELEASE_SHA256
} = {}) {
  assertRegularFile(sourcePath, "PortableGit source archive")
  const sourceSha256 = sha256File(sourcePath)
  if (!SHA256_RE.test(expectedSourceSha256) || sourceSha256 !== expectedSourceSha256) {
    throw new Error(`PortableGit source SHA-256 mismatch: expected ${expectedSourceSha256}, got ${sourceSha256}`)
  }

  mkdirSync(dirname(outputPath), { recursive: true })
  mkdirSync(dirname(provenancePath), { recursive: true })
  const workRoot = mkdtempSync(join(tmpdir(), "hermes-git-runtime-"))
  const extractedRoot = join(workRoot, "extracted")
  mkdirSync(extractedRoot)
  try {
    await extractPortableGit(sourcePath, extractedRoot)
    pruneRuntimeTree(extractedRoot)
    materializeRuntimeHardLinks(extractedRoot)
    for (const required of REQUIRED_RUNTIME_ENTRIES) {
      assertRegularFile(join(extractedRoot, ...required.split("/")), `required Git runtime entry ${required}`)
    }
    createRuntimeArchive(extractedRoot, outputPath)
    const entries = auditGitRuntimeArchive(outputPath)
    const outputStats = statSync(outputPath)
    const provenance = {
      schemaVersion: 1,
      source: {
        file: WINDOWS_PORTABLE_GIT_RELEASE_FILE,
        sha256: sourceSha256
      },
      runtime: {
        file: WINDOWS_GIT_RUNTIME_FILE,
        size: outputStats.size,
        sha256: sha256File(outputPath),
        entries: entries.length
      }
    }
    writeFileSync(provenancePath, JSON.stringify(provenance, null, 2) + "\n", "utf8")
    return provenance
  } finally {
    rmSync(workRoot, { recursive: true, force: true })
  }
}

export async function main() {
  const result = await prepareWindowsGitRuntime()
  console.log(
    `[prepare-windows-git-runtime] wrote ${result.runtime.file} ` +
      `(${result.runtime.size} bytes, ${result.runtime.entries} entries, ${result.runtime.sha256.slice(0, 12)})`
  )
}

if (isMain(import.meta.url)) await main()
