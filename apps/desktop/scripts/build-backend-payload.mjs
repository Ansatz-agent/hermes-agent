/**
 * Build the backend/Setup source payload shipped with Ansatz.
 *
 * The archive is produced from one committed Git tree, never from working-tree
 * files. This keeps the packaged backend aligned with install-stamp.json and
 * prevents local tests or scratch files from leaking into a distributable.
 */

import { createHash } from "node:crypto"
import { execFileSync } from "node:child_process"
import {
  lstatSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync
} from "node:fs"
import { join, resolve } from "node:path"

import { isMain } from "./utils.mjs"
import {
  auditGitRuntimeArchive,
  WINDOWS_GIT_RUNTIME_FILE,
  WINDOWS_GIT_RUNTIME_PROVENANCE_FILE,
  WINDOWS_PORTABLE_GIT_RELEASE_FILE,
  WINDOWS_PORTABLE_GIT_RELEASE_SHA256
} from "./prepare-windows-git-runtime.mjs"

const MANIFEST_SCHEMA_VERSION = 1
const INSTALL_STAMP_SCHEMA_VERSION = 1
const COMMIT_RE = /^[0-9a-f]{40}$/i
const SHA256_RE = /^[0-9a-f]{64}$/

const DESKTOP_ROOT = resolve(import.meta.dirname, "..")
const REPO_ROOT = resolve(DESKTOP_ROOT, "..", "..")
const DEFAULT_STAMP_PATH = join(DESKTOP_ROOT, "build", "install-stamp.json")
const DEFAULT_OUTPUT_DIR = join(DESKTOP_ROOT, "build", "bootstrap")
const DEFAULT_WINDOWS_GIT_RUNTIME_PATH = join(DESKTOP_ROOT, "build", "windows-prereqs", WINDOWS_GIT_RUNTIME_FILE)
const DEFAULT_WINDOWS_GIT_RUNTIME_PROVENANCE_PATH = join(
  DESKTOP_ROOT,
  "build",
  "package-input-evidence",
  WINDOWS_GIT_RUNTIME_PROVENANCE_FILE
)

export function installerFileForPlatform(platform) {
  if (platform === "darwin") return "install.sh"
  if (platform === "win32") return "install.ps1"
  throw new Error(`unsupported Desktop payload platform: ${platform}`)
}

export const RUNTIME_SCRIPT_FILES = Object.freeze([
  "desktop-update.ps1",
  "desktop-update/posix.sh",
  "desktop-update/windows.ps1",
  "discord-voice-doctor.py",
  "hermes-gateway",
  "install.cmd",
  "install.sh",
  "install.ps1",
  "keystroke_diagnostic.py"
])

export const RUNTIME_SCRIPT_DIRECTORIES = Object.freeze(["lib", "whatsapp-bridge"])

export const PAYLOAD_PATHS = Object.freeze([
  "acp_adapter",
  "agent",
  // Setup's Stage-Desktop builds the launchable Electron app from this
  // checkout. Keep the desktop sources in the same sealed snapshot so the
  // standalone Tauri Setup does not need to clone the private repository.
  "apps/desktop",
  // npm ci resolves the root workspaces before Stage-Desktop runs; retain the
  // Setup workspace manifest so a bundled checkout is structurally complete.
  "apps/bootstrap-installer",
  "assets",
  "cron",
  "config/ansatz-voice-trace",
  // Stage-Desktop bundles this allowlist into electron-main.mjs. Keep the
  // committed source JSON beside the desktop checkout in the sealed archive.
  "docs/security/hermes-managed-download-origins.json",
  "desktop_auth_runtime",
  "gateway",
  "hermes",
  "hermes_cli",
  "locales",
  "native",
  "optional-mcps",
  "optional-skills",
  "plugins",
  "providers",
  "skills",
  "tools",
  "tui_gateway",
  "ui-tui",
  "web",
  "apps/shared",
  ...RUNTIME_SCRIPT_FILES.map(relativePath => `scripts/${relativePath}`),
  ...RUNTIME_SCRIPT_DIRECTORIES.map(relativePath => `scripts/${relativePath}`),
  ".env.example",
  ".node-version",
  ".npmrc",
  ".nvmrc",
  ".python-version",
  "LICENSE",
  "batch_runner.py",
  "cli-config.yaml.example",
  "cli.py",
  "eslint.config.shared.mjs",
  "hermes_bootstrap.py",
  "hermes_constants.py",
  "hermes_logging.py",
  "hermes_state.py",
  "hermes_state_common.py",
  "hermes_state_portability.py",
  "hermes_state_schema.py",
  "hermes_state_search.py",
  "hermes_time.py",
  "mcp_serve.py",
  "mini_swe_runner.py",
  "model_tools.py",
  "package-lock.json",
  "package.json",
  "pyproject.toml",
  "registration_lifecycle.py",
  "run_agent.py",
  "setup-hermes.sh",
  "setup.py",
  "toolset_distributions.py",
  "toolsets.py",
  "trajectory_compressor.py",
  "utils.py",
  "uv.lock",
  "uv.toml"
])

export const PAYLOAD_EXCLUDES = Object.freeze([
  ":(glob,exclude)**/tests/**",
  ":(glob,exclude)**/test/**",
  ":(glob,exclude)**/__tests__/**",
  ":(glob,exclude)**/*.test.py",
  ":(glob,exclude)**/*.test.js",
  ":(glob,exclude)**/*.test.mjs",
  ":(glob,exclude)**/*.test.ts",
  ":(glob,exclude)**/*.test.tsx",
  ":(glob,exclude)**/*.spec.py",
  ":(glob,exclude)**/*.spec.js",
  ":(glob,exclude)**/*.spec.mjs",
  ":(glob,exclude)**/*.spec.ts",
  ":(glob,exclude)**/*.spec.tsx",
  ":(glob,exclude)**/test_*.py",
  ":(glob,exclude)**/__pycache__/**",
  // Keep the plugin's dashboard artwork out of the runtime payload while
  // allowing the one top-level security JSON explicitly selected above.
  ":(glob,exclude)plugins/hermes-achievements/docs/**",
  ":(glob,exclude)**/*.test-d.ts",
  ":(glob,exclude)**/*.spec-d.ts",
  ":(glob,exclude)**/.gitignore",
  ":(glob,exclude)**/.gitattributes",
  ":(glob,exclude)**/.gitmodules"
])

export const REQUIRED_ARCHIVE_ENTRIES = Object.freeze([
  "hermes-agent/config/ansatz-voice-trace/plugins.toml",
  "hermes-agent/pyproject.toml",
  "hermes-agent/docs/security/hermes-managed-download-origins.json",
  "hermes-agent/uv.lock",
  "hermes-agent/uv.toml",
  "hermes-agent/desktop_auth_runtime/pyproject.toml",
  "hermes-agent/desktop_auth_runtime/uv.lock",
  "hermes-agent/desktop_auth_runtime/uv.toml",
  "hermes-agent/hermes_cli/main.py",
  "hermes-agent/tools/sensevoice_stt.py",
  "hermes-agent/web/package.json",
  "hermes-agent/ui-tui/package.json",
  "hermes-agent/apps/shared/package.json",
  "hermes-agent/apps/desktop/package.json",
  "hermes-agent/apps/bootstrap-installer/package.json",
  "hermes-agent/scripts/install.ps1",
  "hermes-agent/scripts/install.sh"
])

const FORBIDDEN_PREFIXES = Object.freeze([
  "hermes-agent/tests/",
  "hermes-agent/tests-js/",
  "hermes-agent/website/",
  "hermes-agent/.git/"
])

const TEST_ENTRY_RE =
  /(^|\/)(tests?|__tests__)(\/|$)|\.(test|spec)\.(py|js|mjs|ts|tsx)$|\/test_[^/]*\.py$/
const TEST_SOURCE_RE =
  /(^|\/)(tests?|__tests__)(\/|$)|\.(test|spec)\.(py|js|mjs|ts|tsx)$|(^|\/)test_[^/]*\.py$/
const DECLARATION_TEST_RE = /\.(test|spec)-d\.ts$/
const NESTED_DOCS_RE = /(^|\/)docs(\/|$)/
const GIT_METADATA_RE = /(^|\/)\.(gitignore|gitattributes|gitmodules)$/
const DOWNLOAD_POLICY_DOC = "hermes-agent/docs/security/hermes-managed-download-origins.json"
const DOWNLOAD_POLICY_DOC_DIRS = new Set([
  "hermes-agent/docs/",
  "hermes-agent/docs/security/",
  DOWNLOAD_POLICY_DOC
])
const CI_ONLY_ENTRY_RE =
  /(^|\/)\.github\/|^hermes-agent\/scripts\/(?:desktop-dmg-|verify-desktop-dmg-)|(^|\/)(?:desktop-dmg-credential-login|verify-desktop-dmg-gatekeeper)(?:\.|\/|$)/

function runtimeScriptEntryIsAllowed(entry) {
  const prefix = "hermes-agent/scripts"
  const normalized = entry.replace(/\/$/, "")
  if (normalized === prefix) return true
  if (!normalized.startsWith(`${prefix}/`)) return true

  const relative = normalized.slice(prefix.length + 1)
  if (RUNTIME_SCRIPT_FILES.includes(relative)) return true
  if (RUNTIME_SCRIPT_FILES.some(filePath => filePath.startsWith(`${relative}/`))) return true

  return RUNTIME_SCRIPT_DIRECTORIES.some(
    directory =>
      relative === directory ||
      relative.startsWith(`${directory}/`) ||
      directory.startsWith(`${relative}/`)
  )
}

function runGit(repoRoot, args, options = {}) {
  return execFileSync("git", args, {
    cwd: repoRoot,
    encoding: options.encoding === null ? null : "utf8",
    stdio: ["ignore", "pipe", "pipe"]
  })
}

function readJson(filePath, label) {
  try {
    return JSON.parse(readFileSync(filePath, "utf8"))
  } catch (error) {
    throw new Error(`Cannot read ${label} at ${filePath}: ${error.message}`)
  }
}

export function validateInstallStamp(stamp) {
  if (!stamp || typeof stamp !== "object") {
    throw new Error("install stamp must be a JSON object")
  }
  if (stamp.schemaVersion !== INSTALL_STAMP_SCHEMA_VERSION) {
    throw new Error(`install stamp schemaVersion must be ${INSTALL_STAMP_SCHEMA_VERSION}`)
  }
  if (typeof stamp.commit !== "string" || !COMMIT_RE.test(stamp.commit) || /^0+$/.test(stamp.commit)) {
    throw new Error("install stamp commit must be a real 40-character Git commit")
  }
  if (stamp.dirty !== false) {
    throw new Error("install stamp is dirty; distributable payloads require a clean commit")
  }
  if (stamp.branch !== null && stamp.branch !== undefined && typeof stamp.branch !== "string") {
    throw new Error("install stamp branch must be a string or null")
  }
  return stamp
}

function sha256File(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex")
}

function archiveEntries(archivePath) {
  const output = execFileSync("tar", ["-tzf", archivePath], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"]
  }).trim()
  return output ? output.split(/\r?\n/) : []
}

function entryIsForbidden(entry) {
  return (
    FORBIDDEN_PREFIXES.some(prefix => entry.startsWith(prefix)) ||
    TEST_ENTRY_RE.test(entry) ||
    DECLARATION_TEST_RE.test(entry) ||
    (NESTED_DOCS_RE.test(entry) && !DOWNLOAD_POLICY_DOC_DIRS.has(entry)) ||
    GIT_METADATA_RE.test(entry) ||
    !runtimeScriptEntryIsAllowed(entry)
  )
}

function entryIsCiOnly(entry) {
  return CI_ONLY_ENTRY_RE.test(entry)
}

function sourcePathIsExcluded(sourcePath) {
  return (
    TEST_SOURCE_RE.test(sourcePath) ||
    DECLARATION_TEST_RE.test(sourcePath) ||
    (NESTED_DOCS_RE.test(sourcePath) && sourcePath !== "docs/security/hermes-managed-download-origins.json") ||
    GIT_METADATA_RE.test(sourcePath)
  )
}

function assertNoSelectedSymlinks(repoRoot, commit, payloadPaths) {
  // git archive accepts exclude pathspecs, but git ls-tree does not. List the
  // positive selection and apply the same exclusion contract locally.
  const output = runGit(repoRoot, ["ls-tree", "-r", commit, "--", ...payloadPaths]).trim()
  for (const line of output.split(/\r?\n/)) {
    if (!line) continue
    const match = /^(\d{6})\s+\w+\s+[0-9a-f]+\t(.+)$/.exec(line)
    if (match && !sourcePathIsExcluded(match[2]) && match[1] === "120000") {
      throw new Error(`Backend payload cannot contain symbolic link: ${match[2]}`)
    }
  }
}

function replaceFile(tempPath, finalPath) {
  try {
    unlinkSync(finalPath)
  } catch (error) {
    if (error.code !== "ENOENT") throw error
  }
  renameSync(tempPath, finalPath)
}

export function buildBackendPayload({
  repoRoot = REPO_ROOT,
  stampPath = DEFAULT_STAMP_PATH,
  outputDir = DEFAULT_OUTPUT_DIR,
  payloadPaths = PAYLOAD_PATHS,
  payloadExcludes = PAYLOAD_EXCLUDES,
  requiredEntries = REQUIRED_ARCHIVE_ENTRIES,
  platform = "darwin",
  gitRuntimePath = DEFAULT_WINDOWS_GIT_RUNTIME_PATH,
  gitRuntimeProvenancePath = DEFAULT_WINDOWS_GIT_RUNTIME_PROVENANCE_PATH,
  expectedPortableGitSourceSha256 = WINDOWS_PORTABLE_GIT_RELEASE_SHA256,
  gitRuntimeAudit = auditGitRuntimeArchive
} = {}) {
  const installerFile = installerFileForPlatform(platform)
  const stamp = validateInstallStamp(readJson(stampPath, "install stamp"))
  const head = runGit(repoRoot, ["rev-parse", "HEAD"]).trim()
  if (head !== stamp.commit) {
    throw new Error(`install stamp commit ${stamp.commit} does not match checkout commit ${head}`)
  }
  const dirtyTrackedFiles = runGit(repoRoot, ["status", "--porcelain", "--untracked-files=no"]).trim()
  if (dirtyTrackedFiles) {
    throw new Error("tracked working tree is dirty; distributable payloads require a clean checkout")
  }

  let gitBashRuntime = null
  if (platform === "win32") {
    const runtimeStats = lstatSync(gitRuntimePath)
    if (!runtimeStats.isFile() || runtimeStats.isSymbolicLink()) {
      throw new Error("Bundled Git Bash runtime must be a regular non-link file")
    }
    const provenance = readJson(gitRuntimeProvenancePath, "Git Bash runtime provenance")
    const runtimeSha256 = sha256File(gitRuntimePath)
    const runtimeEntries = gitRuntimeAudit(gitRuntimePath)
    if (
      provenance?.schemaVersion !== 1 ||
      provenance.source?.file !== WINDOWS_PORTABLE_GIT_RELEASE_FILE ||
      provenance.source?.sha256 !== expectedPortableGitSourceSha256 ||
      provenance.runtime?.file !== WINDOWS_GIT_RUNTIME_FILE ||
      provenance.runtime?.size !== runtimeStats.size ||
      provenance.runtime?.sha256 !== runtimeSha256 ||
      provenance.runtime?.entries !== runtimeEntries.length
    ) {
      throw new Error("Bundled Git Bash runtime provenance, size, entries, or SHA-256 is invalid")
    }
    gitBashRuntime = {
      file: WINDOWS_GIT_RUNTIME_FILE,
      size: runtimeStats.size,
      sha256: runtimeSha256,
      entries: runtimeEntries.length,
      source: {
        file: WINDOWS_PORTABLE_GIT_RELEASE_FILE,
        sha256: expectedPortableGitSourceSha256
      }
    }
  }

  assertNoSelectedSymlinks(repoRoot, stamp.commit, payloadPaths)
  mkdirSync(outputDir, { recursive: true })

  const archivePath = join(outputDir, "hermes-backend.tar.gz")
  const installerPath = join(outputDir, installerFile)
  const manifestPath = join(outputDir, "payload-manifest.json")
  const archiveTempPath = `${archivePath}.tmp-${process.pid}`
  const installerTempPath = `${installerPath}.tmp-${process.pid}`
  const manifestTempPath = `${manifestPath}.tmp-${process.pid}`

  try {
    runGit(repoRoot, [
      "archive",
      "--format=tar.gz",
      "--prefix=hermes-agent/",
      `--output=${archiveTempPath}`,
      stamp.commit,
      ...payloadPaths,
      ...payloadExcludes
    ])

    const entries = archiveEntries(archiveTempPath)
    for (const required of requiredEntries) {
      if (!entries.includes(required)) {
        throw new Error(`Backend payload is missing required entry ${required}`)
      }
    }
    const ciOnly = entries.find(entryIsCiOnly)
    if (ciOnly) {
      throw new Error(`Backend payload contains CI-only entry ${ciOnly}`)
    }
    const forbidden = entries.find(entryIsForbidden)
    if (forbidden) {
      throw new Error(`Backend payload contains forbidden entry ${forbidden}`)
    }

    const installer = runGit(repoRoot, ["show", `${stamp.commit}:scripts/${installerFile}`], { encoding: null })
    writeFileSync(installerTempPath, installer)

    const archiveStat = statSync(archiveTempPath)
    const installerStat = statSync(installerTempPath)
    const archiveSha256 = sha256File(archiveTempPath)
    const installerSha256 = sha256File(installerTempPath)
    if (!SHA256_RE.test(archiveSha256) || !SHA256_RE.test(installerSha256)) {
      throw new Error("Backend payload SHA-256 generation failed")
    }

    const manifest = {
      schemaVersion: MANIFEST_SCHEMA_VERSION,
      commit: stamp.commit,
      branch: stamp.branch || null,
      archive: {
        file: "hermes-backend.tar.gz",
        size: archiveStat.size,
        sha256: archiveSha256
      },
      installer: {
        file: installerFile,
        size: installerStat.size,
        sha256: installerSha256
      },
      ...(gitBashRuntime ? { gitBashRuntime } : {})
    }
    writeFileSync(manifestTempPath, JSON.stringify(manifest, null, 2) + "\n", "utf8")

    replaceFile(archiveTempPath, archivePath)
    replaceFile(installerTempPath, installerPath)
    replaceFile(manifestTempPath, manifestPath)

    return { manifest, entries, outputDir }
  } finally {
    for (const filePath of [archiveTempPath, installerTempPath, manifestTempPath]) {
      try {
        unlinkSync(filePath)
      } catch (error) {
        if (error.code !== "ENOENT") {
          console.warn(`[build-backend-payload] could not remove temporary file ${filePath}: ${error.message}`)
        }
      }
    }
  }
}

export function main() {
  const result = buildBackendPayload()
  console.log(
    `[build-backend-payload] wrote ${result.manifest.archive.file} ` +
      `(${result.manifest.archive.size} bytes, ${result.manifest.archive.sha256.slice(0, 12)}) ` +
      `for ${result.manifest.commit.slice(0, 12)}`
  )
}

if (isMain(import.meta.url)) {
  main()
}
