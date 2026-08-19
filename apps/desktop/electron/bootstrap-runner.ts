/**
 * bootstrap-runner.ts
 *
 * Drives apps/desktop's first-launch install of Hermes Agent by spawning
 * scripts/install.ps1 stage-by-stage and streaming progress events back to
 * the renderer.
 *
 * Wired from electron/main.ts:
 *   import { runBootstrap }from './bootstrap-runner'
 *   const result = await runBootstrap({
 *     installStamp,        // INSTALL_STAMP from main.ts (may be null in dev)
 *     activeRoot,          // ACTIVE_HERMES_ROOT
 *     sourceRepoRoot,      // SOURCE_REPO_ROOT (for dev install.ps1 lookup)
 *     hermesHome,          // HERMES_HOME
 *     logRoot,             // HERMES_HOME/logs
 *     emit: ev => {...}    // event sink (sender.send or similar)
 *   })
 *
 * Emits events with shape:
 *   { type: 'manifest',  stages: [{name, title, category, needs_user_input}, ...] }
 *   { type: 'stage',     name, state: 'running'|'succeeded'|'skipped'|'failed',
 *                        json?, durationMs?, error? }
 *   { type: 'log',       stage?, line, stream: 'stdout'|'stderr' } // raw line from install.ps1
 *   { type: 'complete',  marker: <written marker payload> }
 *   { type: 'failed',    stage?, error }     // bootstrap aborted
 *
 * Resolves with the same shape as the final 'complete' or 'failed' event so
 * callers can await either way.
 *
 * NOT implemented yet (deferred to Phase 1E / 1F):
 *   - User-facing retry / cancel from the renderer (event channels exist;
 *     no UI consumes them yet)
 */

import { execFileSync, spawn } from 'node:child_process'
import fs from 'node:fs'
import fsp from 'node:fs/promises'
import https from 'node:https'
import path from 'node:path'

import { prepareBundledSource, type PreparedBundledSource, resolveBundledPayload } from './bootstrap-payload'
import { buildBootstrapEnvironment, runBootstrapProcess } from './bootstrap-process'
import { hiddenWindowsChildOptions } from './windows-child-options'

const IS_WINDOWS = process.platform === 'win32'

const STAMP_COMMIT_RE = /^[0-9a-f]{7,40}$/i
const FALLBACK_COMMIT_RE = /^0{7,40}$/
const FALLBACK_BRANCH = 'main'

const DEFAULT_BOOTSTRAP_TIMEOUTS = Object.freeze({
  idleMs: 90_000,
  killGraceMs: 2_000,
  manifestHardMs: 60_000,
  stageHardMs: 15 * 60_000,
  totalMs: 30 * 60_000
})

function positiveTimeout(value, fallback) {
  return Number.isFinite(value) && value > 0 ? value : fallback
}

function timeoutBudget(totalDeadline, preferredMs) {
  const remainingMs = Math.max(1, Math.ceil(totalDeadline - performance.now()))

  return {
    hardTimeoutMs: Math.min(preferredMs, remainingMs),
    totalLimited: remainingMs <= preferredMs
  }
}

function terminationError(result, { totalLimited = false } = {}) {
  if (result.termination === 'cancelled') {
    return 'BOOTSTRAP_CANCELLED'
  }

  if (result.termination === 'idle-timeout') {
    return 'BOOTSTRAP_IDLE_TIMEOUT'
  }

  if (result.termination === 'hard-timeout') {
    return totalLimited ? 'BOOTSTRAP_TOTAL_TIMEOUT' : 'BOOTSTRAP_STAGE_TIMEOUT'
  }

  return null
}

function isPinnedCommit(commit) {
  return typeof commit === 'string' && STAMP_COMMIT_RE.test(commit) && !FALLBACK_COMMIT_RE.test(commit)
}

type ExecGitFn = (args: string[], cwd: string) => string
type ResolveHeadFn = (activeRoot: string | null | undefined) => string | null

/**
 * Read HEAD from a managed checkout. Used after bootstrap so fallback
 * (all-zero) install stamps still produce a marker that
 * isBootstrapComplete() accepts (pinnedCommit length >= 7).
 */
function resolveCheckoutHead(activeRoot: string | null | undefined, opts: { execGit?: ExecGitFn } = {}): string | null {
  if (!activeRoot) {
    return null
  }

  const run: ExecGitFn =
    opts.execGit ||
    ((args, cwd) =>
      execFileSync('git', args, {
        cwd,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
        timeout: 15_000,
        ...hiddenWindowsChildOptions()
      }).trim())

  try {
    const sha = run(['-c', 'windows.appendAtomically=false', 'rev-parse', 'HEAD'], activeRoot)

    return isPinnedCommit(sha) ? sha : null
  } catch {
    return null
  }
}

/** Prefer a real pin already written by install.ps1's bootstrap-marker stage. */
function readExistingPinnedCommit(activeRoot: string | null | undefined): string | null {
  if (!activeRoot) {
    return null
  }

  try {
    const raw = fs.readFileSync(path.join(activeRoot, '.hermes-bootstrap-complete'), 'utf8')
    const parsed = JSON.parse(raw)

    return parsed && isPinnedCommit(parsed.pinnedCommit) ? parsed.pinnedCommit : null
  } catch {
    return null
  }
}

/**
 * Pick the commit to store on the bootstrap-complete marker.
 * Packaged fallback stamps must NOT win (all-zero is not a real pin); after a
 * successful install the checkout's HEAD (or install.ps1's marker) does.
 */
function resolveMarkerPinnedCommit(
  installStamp: { commit?: string; branch?: string | null } | null | undefined,
  activeRoot: string | null | undefined,
  opts: { resolveHead?: ResolveHeadFn } = {}
): string | null {
  const resolveHead = opts.resolveHead || resolveCheckoutHead

  if (installStamp && isPinnedCommit(installStamp.commit)) {
    return installStamp.commit
  }

  const head = resolveHead(activeRoot)

  if (head) {
    return head
  }

  return readExistingPinnedCommit(activeRoot)
}

/**
 * Map an install stamp to the GitHub ref used to fetch install.ps1/sh.
 * Real CI/git stamps pin an immutable SHA. Non-git fallback stamps carry an
 * all-zero placeholder -- treat those as an unpinned branch ref so bootstrap
 * never asks GitHub for commit 0000000... (#50823).
 */
function installRefForStamp(installStamp) {
  if (installStamp && isPinnedCommit(installStamp.commit)) {
    return {
      ref: installStamp.commit,
      cacheKey: installStamp.commit,
      pinned: true
    }
  }

  if (installStamp && typeof installStamp.commit === 'string' && FALLBACK_COMMIT_RE.test(installStamp.commit)) {
    const ref = installStamp.branch || FALLBACK_BRANCH

    return {
      ref,
      cacheKey: `fallback-${String(ref).replace(/[^0-9A-Za-z._-]/g, '_')}`,
      pinned: false
    }
  }

  return null
}

// Stages flagged needs_user_input=true in the manifest are skipped by the
// runner (passed -NonInteractive to install.ps1, which the install script
// itself handles by emitting skipped=true frames). The renderer / 1E onboarding
// overlay takes over for those concerns (API keys, model, persona, gateway).
// We let install.ps1's own -NonInteractive logic drive this rather than
// filtering client-side -- single source of truth.

// ---------------------------------------------------------------------------
// install.ps1 source resolution
// ---------------------------------------------------------------------------

function installScriptName() {
  return process.platform === 'win32' ? 'install.ps1' : 'install.sh'
}

function installScriptKind() {
  return process.platform === 'win32' ? 'powershell' : 'posix'
}

function resolveLocalInstallScript(sourceRepoRoot) {
  if (!sourceRepoRoot) {
    return null
  }

  const candidate = path.join(sourceRepoRoot, 'scripts', installScriptName())

  try {
    fs.accessSync(candidate, fs.constants.R_OK)

    return candidate
  } catch {
    return null
  }
}

function bootstrapCacheDir(hermesHome) {
  return path.join(hermesHome, 'bootstrap-cache')
}

// The install.sh / install.ps1 that ships inside the already-installed agent
// checkout under ~/.hermes/hermes-agent. Used as a last-resort fallback when
// the pinned commit can't be fetched from GitHub (e.g. a locally-built desktop
// app stamped to an unpushed HEAD).
function installedAgentInstallScript(hermesHome) {
  if (!hermesHome) {
    return null
  }

  const candidate = path.join(hermesHome, 'hermes-agent', 'scripts', installScriptName())

  try {
    fs.accessSync(candidate, fs.constants.R_OK)

    return candidate
  } catch {
    return null
  }
}

function hasExistingGitCheckout(activeRoot) {
  if (!activeRoot) {
    return false
  }

  try {
    return fs.existsSync(path.join(activeRoot, '.git'))
  } catch {
    return false
  }
}

function cachedScriptPath(hermesHome, commit) {
  return path.join(bootstrapCacheDir(hermesHome), `install-${commit}.${process.platform === 'win32' ? 'ps1' : 'sh'}`)
}

function downloadInstallScript(ref, destPath) {
  // Fetch from GitHub raw at the install ref. Normal production builds pass a
  // pinned SHA (immutable). Non-git fallback builds pass an unpinned branch
  // ref so local builds can still bootstrap without pretending the all-zero
  // placeholder is a real GitHub commit.
  const scriptName = installScriptName()
  const url = `https://raw.githubusercontent.com/NousResearch/hermes-agent/${ref}/scripts/${scriptName}`

  return new Promise((resolve, reject) => {
    fs.mkdirSync(path.dirname(destPath), { recursive: true })
    const tmpPath = destPath + '.tmp'
    const out = fs.createWriteStream(tmpPath)
    https
      .get(url, res => {
        if (res.statusCode === 301 || res.statusCode === 302) {
          // GitHub raw shouldn't redirect for a SHA URL, but follow once
          // defensively.
          out.close()
          fs.unlinkSync(tmpPath)
          https
            .get(res.headers.location, res2 => {
              if (res2.statusCode !== 200) {
                reject(
                  new Error(
                    `Failed to download ${scriptName}: HTTP ${res2.statusCode} from redirect ${res.headers.location}`
                  )
                )

                return
              }

              const out2 = fs.createWriteStream(tmpPath)
              res2.pipe(out2)
              out2.on('finish', () => {
                out2.close()
                fs.renameSync(tmpPath, destPath)
                resolve(destPath)
              })
              out2.on('error', reject)
            })
            .on('error', reject)

          return
        }

        if (res.statusCode !== 200) {
          out.close()

          try {
            fs.unlinkSync(tmpPath)
          } catch {
            void 0
          }

          reject(new Error(`Failed to download ${scriptName}: HTTP ${res.statusCode} from ${url}`))

          return
        }

        res.pipe(out)
        out.on('finish', () => {
          out.close()
          fs.renameSync(tmpPath, destPath)
          resolve(destPath)
        })
        out.on('error', err => {
          try {
            fs.unlinkSync(tmpPath)
          } catch {
            void 0
          }

          reject(err)
        })
      })
      .on('error', err => {
        try {
          fs.unlinkSync(tmpPath)
        } catch {
          void 0
        }

        reject(err)
      })
  })
}

async function resolveInstallScript({
  installStamp,
  sourceRepoRoot,
  hermesHome,
  bundledBootstrapRoot = null,
  emit,
  _download = downloadInstallScript
}) {
  // 1. Dev shortcut: prefer a local checkout's installer so we can iterate
  //    without pushing. SOURCE_REPO_ROOT comes from main.ts (path.resolve
  //    of APP_ROOT/../..).
  const localScript = resolveLocalInstallScript(sourceRepoRoot)

  if (localScript) {
    emit({ type: 'log', line: `[bootstrap] using local ${installScriptName()} at ${localScript}` })

    return { path: localScript, source: 'local', kind: installScriptKind() }
  }

  // 2. Packaged Desktop path: the installer is part of the signed app
  // resources. A missing/corrupt package must fail locally instead of quietly
  // reintroducing the raw.githubusercontent.com first-launch dependency.
  if (bundledBootstrapRoot) {
    const bundledScript = path.join(bundledBootstrapRoot, installScriptName())

    try {
      const stats = fs.lstatSync(bundledScript)

      if (!stats.isFile() || stats.isSymbolicLink()) {
        throw new Error('not a regular file')
      }
    } catch (err) {
      throw new Error(
        `Packaged Hermes is missing bundled ${installScriptName()} at ${bundledScript}. ` +
          `Reinstall or re-download the Hermes DMG. (${err.message})`
      )
    }

    emit({ type: 'log', line: `[bootstrap] using bundled ${installScriptName()} at ${bundledScript}` })

    return { path: bundledScript, source: 'bundled', commit: installStamp?.commit || null, kind: installScriptKind() }
  }

  // 3. Backward-compatible non-packaged path: download from GitHub at the install stamp's ref.
  // Non-git fallback builds carry an all-zero commit; treat that as an
  // unpinned branch ref instead of trying to fetch a non-existent SHA.
  const installRef = installRefForStamp(installStamp)

  if (!installRef) {
    throw new Error(
      `Cannot resolve ${installScriptName()}: no SOURCE_REPO_ROOT and no install stamp. ` +
        'This packaged build was produced without a valid build-time stamp.'
    )
  }

  const cached = cachedScriptPath(hermesHome, installRef.cacheKey)
  const resolvedCommit = installRef.pinned ? installRef.ref : null

  try {
    await fsp.access(cached, fs.constants.R_OK)
    emit({
      type: 'log',
      line: `[bootstrap] using cached ${installScriptName()} for ${installRef.ref.slice(0, 12)}`
    })

    return { path: cached, source: 'cache', commit: resolvedCommit, kind: installScriptKind() }
  } catch {
    // not cached; download
  }

  emit({
    type: 'log',
    line:
      `[bootstrap] fetching ${installScriptName()} for ${installRef.ref.slice(0, 12)} from GitHub` +
      (installRef.pinned ? '' : ' (fallback, unpinned)')
  })

  try {
    await _download(installRef.ref, cached)
    emit({ type: 'log', line: `[bootstrap] saved to ${cached}` })

    return { path: cached, source: 'download', commit: resolvedCommit, kind: installScriptKind() }
  } catch (err) {
    // The pinned commit may not be fetchable from GitHub -- most commonly a
    // locally-built desktop app stamped to an unpushed HEAD (see
    // write-build-stamp.mjs fromLocalGit). Fall back to the installer that
    // ships inside the already-installed agent checkout so dev/self-builds can
    // still bootstrap instead of dying with a fatal 404.
    const installed = installedAgentInstallScript(hermesHome)

    if (installed) {
      emit({
        type: 'log',
        line:
          `[bootstrap] GitHub fetch failed (${err.message}); ` +
          `falling back to installed agent ${installScriptName()} at ${installed}`
      })

      try {
        fs.mkdirSync(path.dirname(cached), { recursive: true })
        fs.copyFileSync(installed, cached)

        return { path: cached, source: 'installed-agent', commit: resolvedCommit, kind: installScriptKind() }
      } catch {
        // Cache copy failed (read-only FS, etc.) -- use the source path directly.
        return { path: installed, source: 'installed-agent', commit: resolvedCommit, kind: installScriptKind() }
      }
    }

    throw err
  }
}

// ---------------------------------------------------------------------------
// powershell wrapper
// ---------------------------------------------------------------------------

// Canonical PowerShell 5.1 location under a Windows root (%SystemRoot%).
function powershellUnderRoot(root) {
  return path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

// Resolve the PowerShell interpreter to spawn.
//
// Spawning bare 'powershell.exe' trusts PATH to contain
// %SystemRoot%\System32\WindowsPowerShell\v1.0. On machines whose PATH was
// trimmed, truncated, or stored as a non-expanding REG_SZ (so %SystemRoot%
// never expands), that lookup fails and the spawn dies with ENOENT before
// install.ps1 ever runs — the installer stalls at "0 of 0 steps". Resolve by
// absolute path first, then fall back to PATH (powershell 5.1, then pwsh 7),
// then a bare name as a last resort.
function resolveWindowsPowerShell() {
  for (const v of ['SystemRoot', 'windir']) {
    const root = process.env[v]

    if (root) {
      const candidate = powershellUnderRoot(root)

      try {
        if (fs.statSync(candidate).isFile()) {
          return candidate
        }
      } catch {
        void 0
      }
    }
  }

  const pathDirs = (process.env.PATH || process.env.Path || '').split(path.delimiter).filter(Boolean)

  for (const exe of ['powershell.exe', 'pwsh.exe']) {
    for (const dir of pathDirs) {
      const candidate = path.join(dir, exe)

      try {
        if (fs.statSync(candidate).isFile()) {
          return candidate
        }
      } catch {
        void 0
      }
    }
  }

  return 'powershell.exe'
}

function spawnPowerShell(
  scriptPath,
  args,
  { emit, stageName, abortSignal, hermesHome, hardTimeoutMs, idleTimeoutMs, killGraceMs }: any = {}
) {
  const ps = process.platform === 'win32' ? resolveWindowsPowerShell() : 'pwsh'

  return runBootstrapProcess({
    command: ps,
    args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath, ...args],
    abortSignal,
    emit,
    env: buildBootstrapEnvironment(process.env, { hermesHome }),
    hardTimeoutMs,
    idleTimeoutMs,
    killGraceMs,
    stageName
  })
}

function spawnBash(
  scriptPath,
  args,
  { emit, stageName, abortSignal, hermesHome, hardTimeoutMs, idleTimeoutMs, killGraceMs }: any = {}
) {
  return runBootstrapProcess({
    command: 'bash',
    args: [scriptPath, ...args],
    abortSignal,
    emit,
    env: buildBootstrapEnvironment(process.env, { hermesHome }),
    hardTimeoutMs,
    idleTimeoutMs,
    killGraceMs,
    stageName
  })
}

// ---------------------------------------------------------------------------
// Manifest + stage dispatch
// ---------------------------------------------------------------------------

// Build the installer branch/pin args from the install stamp. The commit pin
// is fresh-install only: once a managed checkout already exists, bootstrap is
// a repair/update path and must not let an old packaged app detach the checkout
// back to the commit baked into that app. All-zero fallback stamps are never
// passed as -Commit/--commit — only the branch is used (#50823 / #50864 review).
function buildPinArgs(installStamp, { pinCommit = true } = {}) {
  const args = []

  if (pinCommit && installStamp && isPinnedCommit(installStamp.commit)) {
    args.push('-Commit', installStamp.commit)
  }

  if (installStamp && installStamp.branch) {
    args.push('-Branch', installStamp.branch)
  }

  return args
}

function buildPosixPinArgs({ installStamp, activeRoot, hermesHome, pinCommit = true, bundledSource = false }) {
  const args = ['--dir', activeRoot, '--hermes-home', hermesHome]

  if (installStamp && installStamp.branch) {
    args.push('--branch', installStamp.branch)
  }

  if (pinCommit && installStamp && isPinnedCommit(installStamp.commit)) {
    args.push('--commit', installStamp.commit)
  }

  if (bundledSource) {
    // The bundled voice runtime only needs the package registries and its
    // ModelScope/GitHub model mirror ladder. Avoid the installer's unrelated,
    // best-effort GitHub fetch for the optional Computer Use driver; that
    // capability still installs on demand if the user enables it later.
    args.push('--bundled-source', '--skip-computer-use')
  }

  return args
}

async function fetchManifest({
  scriptPath,
  installerKind,
  emit,
  hermesHome,
  activeRoot,
  installStamp,
  pinCommit,
  bundledSource,
  abortSignal,
  timeoutBudget: processTimeout,
  idleTimeoutMs,
  killGraceMs
}) {
  const isPosix = installerKind === 'posix'

  const args = isPosix
    ? ['--manifest', ...buildPosixPinArgs({ installStamp, activeRoot, hermesHome, pinCommit, bundledSource })]
    : ['-Manifest', ...buildPinArgs(installStamp, { pinCommit })]

  const result = await (isPosix ? spawnBash : spawnPowerShell)(scriptPath, args, {
    abortSignal,
    emit,
    stageName: '__manifest__',
    hermesHome,
    hardTimeoutMs: processTimeout.hardTimeoutMs,
    idleTimeoutMs,
    killGraceMs
  })

  const terminalError = terminationError(result, processTimeout)

  if (terminalError) {
    throw new Error(terminalError)
  }

  if (result.code !== 0) {
    throw new Error(`${isPosix ? 'install.sh --manifest' : 'install.ps1 -Manifest'} failed: exit ${result.code}`)
  }

  // The manifest is the LAST JSON line on stdout (install.ps1 may print
  // banner / info lines first depending on Console.OutputEncoding effects).
  // Find the last line that parses as JSON with a `stages` field.
  const lines = result.stdout.split(/\r?\n/).filter(Boolean)

  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      const parsed = JSON.parse(lines[i])

      if (parsed && Array.isArray(parsed.stages)) {
        return parsed
      }
    } catch {
      void 0
    }
  }

  throw new Error(
    `${isPosix ? 'install.sh --manifest' : 'install.ps1 -Manifest'} produced no parseable JSON payload\n${result.stdout}`
  )
}

// Parse the JSON result frame from a stage run. The protocol guarantees
// exactly one JSON line per stage in -Json or -Stage mode (post #27224 fix
// for the double-emit bug we addressed in the install.ps1 PR).
function parseStageResult(stdout) {
  const lines = stdout.split(/\r?\n/).filter(Boolean)

  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      const parsed = JSON.parse(lines[i])

      if (parsed && typeof parsed.ok === 'boolean' && typeof parsed.stage === 'string') {
        return parsed
      }
    } catch {
      void 0
    }
  }

  return null
}

async function runStage({
  scriptPath,
  installerKind,
  stage,
  emit,
  hermesHome,
  activeRoot,
  abortSignal,
  installStamp,
  pinCommit,
  bundledSource,
  timeoutBudget: processTimeout,
  idleTimeoutMs,
  killGraceMs
}) {
  const startedAt = Date.now()
  emit({ type: 'stage', name: stage.name, state: 'running' })

  const isPosix = installerKind === 'posix'

  const args = isPosix
    ? [
        '--stage',
        stage.name,
        '--non-interactive',
        '--json',
        ...buildPosixPinArgs({ installStamp, activeRoot, hermesHome, pinCommit, bundledSource })
      ]
    : ['-Stage', stage.name, '-NonInteractive', '-Json', ...buildPinArgs(installStamp, { pinCommit })]

  const result = await (isPosix ? spawnBash : spawnPowerShell)(scriptPath, args, {
    emit,
    stageName: stage.name,
    abortSignal,
    hermesHome,
    hardTimeoutMs: processTimeout.hardTimeoutMs,
    idleTimeoutMs,
    killGraceMs
  })

  const durationMs = Date.now() - startedAt

  const terminalError = terminationError(result, processTimeout)

  if (terminalError) {
    const ev = {
      type: 'stage',
      name: stage.name,
      state: 'failed',
      durationMs,
      error: terminalError,
      cancelled: terminalError === 'BOOTSTRAP_CANCELLED'
    }

    emit(ev)

    return ev
  }

  const json = parseStageResult(result.stdout)

  if (!json) {
    const ev = {
      type: 'stage',
      name: stage.name,
      state: 'failed',
      durationMs,
      error: `${isPosix ? 'install.sh --stage' : 'install.ps1 -Stage'} ${stage.name} produced no JSON result frame (exit=${result.code})`,
      json: null
    }

    emit(ev)

    return ev
  }

  if (json.ok && json.skipped) {
    const ev = { type: 'stage', name: stage.name, state: 'skipped', durationMs, json }
    emit(ev)

    return ev
  }

  if (json.ok) {
    const ev = { type: 'stage', name: stage.name, state: 'succeeded', durationMs, json }
    emit(ev)

    return ev
  }

  const ev = {
    type: 'stage',
    name: stage.name,
    state: 'failed',
    durationMs,
    json,
    error: json.reason || `exit code ${result.code}`
  }

  emit(ev)

  return ev
}

// ---------------------------------------------------------------------------
// Per-run log file
// ---------------------------------------------------------------------------

function openRunLog(logRoot) {
  fs.mkdirSync(logRoot, { recursive: true })
  const ts = new Date().toISOString().replace(/[:.]/g, '-')
  const logPath = path.join(logRoot, `bootstrap-${ts}.log`)
  const stream = fs.createWriteStream(logPath, { flags: 'a' })

  return { path: logPath, stream }
}

function bundledRuntimePython(activeRoot) {
  return process.platform === 'win32'
    ? path.join(activeRoot, 'venv', 'Scripts', 'python.exe')
    : path.join(activeRoot, 'venv', 'bin', 'python')
}

function validateBundledRuntime(activeRoot) {
  const python = bundledRuntimePython(activeRoot)

  try {
    const stats = fs.statSync(python)

    if (!stats.isFile()) {
      throw new Error('not a regular file')
    }
  } catch (err) {
    return Promise.reject(new Error(`Bundled runtime validation failed: Python is unavailable at ${python}`))
  }

  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-c', 'import hermes_cli'], {
      stdio: ['ignore', 'ignore', 'pipe'],
      env: {
        ...process.env,
        PYTHONPATH: [activeRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)
      }
    })

    let stderr = ''
    let settled = false

    const timeout = setTimeout(() => {
      if (settled) {
        return
      }

      settled = true

      try {
        child.kill('SIGTERM')
      } catch {
        void 0
      }

      reject(new Error('Bundled runtime import smoke check timed out'))
    }, 30_000)

    child.stderr.setEncoding('utf8')
    child.stderr.on('data', chunk => {
      stderr += chunk
    })
    child.on('error', err => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(timeout)
      reject(new Error(`Bundled runtime import smoke check failed: ${err.message}`))
    })
    child.on('close', code => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(timeout)

      if (code === 0) {
        resolve(true)
      } else {
        reject(
          new Error(
            `Bundled runtime import smoke check failed with exit ${code}` +
              (stderr.trim() ? `: ${stderr.trim()}` : '')
          )
        )
      }
    })
  })
}

// ---------------------------------------------------------------------------
// Public entrypoint
// ---------------------------------------------------------------------------

async function runBootstrap(opts) {
  const {
    installStamp,
    activeRoot,
    sourceRepoRoot,
    bundledBootstrapRoot,
    hermesHome,
    logRoot,
    onEvent,
    abortSignal,
    writeMarker, // callback to write the bootstrap-complete marker; main.ts provides
    timeouts: timeoutOverrides = {}
  } = opts

  const timeouts = {
    idleMs: positiveTimeout(timeoutOverrides.idleMs, DEFAULT_BOOTSTRAP_TIMEOUTS.idleMs),
    killGraceMs: positiveTimeout(timeoutOverrides.killGraceMs, DEFAULT_BOOTSTRAP_TIMEOUTS.killGraceMs),
    manifestHardMs: positiveTimeout(timeoutOverrides.manifestHardMs, DEFAULT_BOOTSTRAP_TIMEOUTS.manifestHardMs),
    stageHardMs: positiveTimeout(timeoutOverrides.stageHardMs, DEFAULT_BOOTSTRAP_TIMEOUTS.stageHardMs),
    totalMs: positiveTimeout(timeoutOverrides.totalMs, DEFAULT_BOOTSTRAP_TIMEOUTS.totalMs)
  }

  const totalDeadline = performance.now() + timeouts.totalMs

  // Bail before spawning anything if the user already cancelled — otherwise an
  // already-aborted signal would still fetch the manifest (a spawn) before the
  // in-loop abort check fires.
  if (abortSignal && abortSignal.aborted) {
    if (typeof onEvent === 'function') {
      try {
        onEvent({ type: 'failed', error: 'bootstrap cancelled by user' })
      } catch {
        void 0
      }
    }

    return { ok: false, cancelled: true }
  }

  const runLog = openRunLog(logRoot || path.join(hermesHome, 'logs'))

  // Tee every event to the runLog AND the caller's onEvent. This gives us a
  // forensic trail per bootstrap run AND lets the renderer subscribe live.
  const emit = ev => {
    try {
      runLog.stream.write(JSON.stringify(ev) + '\n')
    } catch {
      void 0
    }

    try {
      if (typeof onEvent === 'function') {
        onEvent(ev)
      }
    } catch (err) {
      // Don't let a subscriber bug crash the bootstrap
      runLog.stream.write(`emit error: ${err && err.message}\n`)
    }
  }

  emit({
    type: 'log',
    line:
      `[bootstrap] starting at ${new Date().toISOString()}; ` +
      `activeRoot=${activeRoot}; ` +
      `stamp=${installStamp ? installStamp.commit.slice(0, 12) : '<none>'}; ` +
      `runLog=${runLog.path}`
  })

  let sourceTransaction: PreparedBundledSource | null = null

  try {
    const existingCheckout = hasExistingGitCheckout(activeRoot)
    const pinCommit = !existingCheckout
    let bundledSource = false

    if (existingCheckout && installStamp && installStamp.commit) {
      emit({
        type: 'log',
        line:
          `[bootstrap] existing checkout detected at ${activeRoot}; ` +
          `not pinning to packaged install stamp ${installStamp.commit.slice(0, 12)}`
      })
    }

    const payload = bundledBootstrapRoot
      ? await resolveBundledPayload({ bootstrapRoot: bundledBootstrapRoot, installStamp })
      : null

    if (payload && !existingCheckout) {
      sourceTransaction = await prepareBundledSource({ payload, activeRoot, hermesHome })
      bundledSource = sourceTransaction.kind !== 'existing-git'
      emit({
        type: 'log',
        line:
          `[bootstrap] prepared bundled backend ${payload.manifest.commit.slice(0, 12)} ` +
          `(${sourceTransaction.kind})`
      })
    }

    // 1. Resolve the platform installer.
    const scriptInfo = await resolveInstallScript({
      installStamp,
      sourceRepoRoot,
      hermesHome,
      bundledBootstrapRoot,
      emit
    })

    const installerKind = scriptInfo.kind || 'powershell'

    // 2. Fetch manifest
    const manifestTimeout = timeoutBudget(totalDeadline, timeouts.manifestHardMs)

    const manifest = await fetchManifest({
      scriptPath: scriptInfo.path,
      installerKind,
      emit,
      hermesHome,
      activeRoot,
      installStamp,
      pinCommit,
      bundledSource,
      abortSignal,
      timeoutBudget: manifestTimeout,
      idleTimeoutMs: timeouts.idleMs,
      killGraceMs: timeouts.killGraceMs
    })

    emit({
      type: 'manifest',
      stages: manifest.stages,
      protocolVersion: manifest.protocol_version || manifest.protocolVersion || null
    })

    // 3. Iterate stages in order. Stages flagged needs_user_input are still
    //    invoked -- install.ps1's own -NonInteractive handler in those stages
    //    emits skipped=true. We trust the protocol rather than filtering
    //    client-side.
    for (const stage of manifest.stages) {
      if (abortSignal && abortSignal.aborted) {
        if (sourceTransaction) {
          await sourceTransaction.rollback()
        }

        emit({ type: 'failed', error: 'bootstrap cancelled by user' })

        return { ok: false, cancelled: true }
      }

      if (performance.now() >= totalDeadline) {
        if (sourceTransaction) {
          await sourceTransaction.rollback()
        }

        emit({ type: 'failed', stage: stage.name, error: 'BOOTSTRAP_TOTAL_TIMEOUT' })

        return { ok: false, failedStage: stage.name, error: 'BOOTSTRAP_TOTAL_TIMEOUT' }
      }

      const stageTimeout = timeoutBudget(totalDeadline, timeouts.stageHardMs)

      const ev = await runStage({
        scriptPath: scriptInfo.path,
        installerKind,
        stage,
        emit,
        hermesHome,
        activeRoot,
        abortSignal,
        installStamp,
        pinCommit,
        bundledSource,
        timeoutBudget: stageTimeout,
        idleTimeoutMs: timeouts.idleMs,
        killGraceMs: timeouts.killGraceMs
      })

      if (ev.state === 'failed') {
        if (sourceTransaction) {
          await sourceTransaction.rollback()
        }

        emit({ type: 'failed', stage: stage.name, error: (ev as any).error || 'stage failed' })

        if ((ev as any).cancelled) {
          return { ok: false, cancelled: true }
        }

        return { ok: false, failedStage: stage.name, error: (ev as any).error }
      }
    }

    if (bundledSource) {
      await validateBundledRuntime(activeRoot)
    }

    // 4. Write the bootstrap-complete marker. Fallback (all-zero) stamps are
    // not real pins -- resolve HEAD from the checkout we just installed so
    // isBootstrapComplete() (pinnedCommit.length >= 7) accepts the marker
    // instead of re-running bootstrap on every launch (#50823 review).
    const pinnedCommit = resolveMarkerPinnedCommit(installStamp, activeRoot)

    if (!pinnedCommit) {
      emit({
        type: 'log',
        line:
          '[bootstrap] WARNING: could not resolve a real pinnedCommit for the ' +
          'bootstrap-complete marker; subsequent launches may re-run bootstrap'
      })
    } else if (installStamp && !isPinnedCommit(installStamp.commit)) {
      emit({
        type: 'log',
        line: `[bootstrap] fallback stamp resolved marker pin to ${pinnedCommit.slice(0, 12)} from checkout`
      })
    }

    const markerPayload = {
      pinnedCommit,
      pinnedBranch: installStamp ? installStamp.branch : null
    }

    const marker = typeof writeMarker === 'function' ? writeMarker(markerPayload) : markerPayload

    if (sourceTransaction) {
      await sourceTransaction.finalize()
    }

    emit({ type: 'complete', marker })

    return { ok: true, marker }
  } catch (err) {
    if (sourceTransaction) {
      try {
        await sourceTransaction.rollback()
      } catch (rollbackError) {
        emit({
          type: 'log',
          line: `[bootstrap] bundled source rollback failed: ${rollbackError.message || String(rollbackError)}`
        })
      }
    }

    emit({ type: 'failed', error: err.message || String(err) })

    return { ok: false, error: err.message || String(err) }
  } finally {
    try {
      runLog.stream.end()
    } catch {
      void 0
    }
  }
}

export {
  buildPinArgs,
  buildPosixPinArgs,
  cachedScriptPath,
  hasExistingGitCheckout,
  installedAgentInstallScript,
  installRefForStamp,
  isPinnedCommit,
  // Exposed for testability
  parseStageResult,
  resolveCheckoutHead,
  resolveInstallScript,
  resolveLocalInstallScript,
  resolveMarkerPinnedCommit,
  runBootstrap
}
