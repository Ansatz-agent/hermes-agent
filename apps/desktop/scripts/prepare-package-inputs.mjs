import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildAuthToolchain, verifyAuthToolchain } from './build-auth-toolchain.mjs'
import { buildBackendPayload } from './build-backend-payload.mjs'
import {
  prepareAuthToolchainInputsFromEnvironment,
  prepareWindowsAuthToolchainInputsFromEnvironment
} from './prepare-auth-toolchain-inputs.mjs'
import { auditGitRuntimeArchive, prepareWindowsGitRuntime } from './prepare-windows-git-runtime.mjs'

const TARGETS = Object.freeze({
  'darwin-arm64': Object.freeze({
    platform: 'darwin',
    arch: 'arm64',
    installer: 'install.sh',
    outputs: [
      'build/bootstrap/install.sh',
      'build/bootstrap/hermes-backend.tar.gz',
      'build/bootstrap/payload-manifest.json',
      'build/bootstrap/auth-toolchain/manifest.json'
    ],
    resources: [
      { from: 'build/bootstrap/install.sh', to: 'bootstrap/install.sh' },
      { from: 'build/bootstrap/hermes-backend.tar.gz', to: 'bootstrap/hermes-backend.tar.gz' },
      { from: 'build/bootstrap/payload-manifest.json', to: 'bootstrap/payload-manifest.json' },
      { from: 'build/bootstrap/auth-toolchain', to: 'bootstrap/auth-toolchain' }
    ]
  }),
  'win32-x64': Object.freeze({
    platform: 'win32',
    arch: 'x64',
    installer: 'install.ps1',
    outputs: [
      'build/bootstrap/install.ps1',
      'build/bootstrap/hermes-backend.tar.gz',
      'build/bootstrap/payload-manifest.json',
      'build/bootstrap/auth-toolchain/manifest.json',
      'build/windows-prereqs/git-bash-runtime.tar.xz',
      'build/bootstrap/get-windows-win32-x64.tar.gz'
    ],
    resources: [
      { from: 'build/bootstrap/install.ps1', to: 'bootstrap/install.ps1' },
      { from: 'build/bootstrap/hermes-backend.tar.gz', to: 'bootstrap/hermes-backend.tar.gz' },
      { from: 'build/bootstrap/payload-manifest.json', to: 'bootstrap/payload-manifest.json' },
      { from: 'build/bootstrap/auth-toolchain', to: 'bootstrap/auth-toolchain' },
      { from: 'build/windows-prereqs/git-bash-runtime.tar.xz', to: 'bootstrap/git-bash-runtime.tar.xz' },
      { from: 'build/windows-prereqs/get-windows-win32-x64.tar.gz', to: 'bootstrap/get-windows-win32-x64.tar.gz' }
    ]
  })
})

function targetFor(platform, arch) {
  const target = TARGETS[`${platform}-${arch}`]
  if (!target) throw new Error(`unsupported package target: ${platform}-${arch}`)
  return target
}

function validateRoots(repoRoot, desktopRoot) {
  if (!path.isAbsolute(repoRoot) || !path.isAbsolute(desktopRoot)) {
    throw new Error('package input roots must be absolute')
  }
  const expectedDesktop = path.join(path.resolve(repoRoot), 'apps', 'desktop')
  if (path.resolve(desktopRoot) !== expectedDesktop) {
    throw new Error('desktopRoot must be the apps/desktop directory under repoRoot')
  }
}

export function packageInputPlan({ platform, arch, repoRoot, desktopRoot }) {
  validateRoots(repoRoot, desktopRoot)
  const target = targetFor(platform, arch)
  return { platform, arch, outputs: [...target.outputs] }
}

export function packageResourcePlan({ platform, arch }) {
  return targetFor(platform, arch).resources.map(resource => ({ ...resource }))
}

function sha256File(filePath) {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function readJson(filePath, label) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (error) {
    throw new Error(`cannot read ${label}: ${error.message}`)
  }
}

function assertRegularFile(filePath, label) {
  const stats = fs.lstatSync(filePath)
  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link file`)
  }
  return stats
}

function copyWindowsGetWindowsBundle({ sourcePath, destinationPath }) {
  const stats = assertRegularFile(sourcePath, 'bundled get-windows Windows payload')
  if (stats.size < 1024) {
    throw new Error(`bundled get-windows Windows payload is unexpectedly small: ${sourcePath}`)
  }
  fs.mkdirSync(path.dirname(destinationPath), { recursive: true })
  fs.copyFileSync(sourcePath, destinationPath)
}

function verifyPayload(outputDir, installer) {
  const manifest = readJson(path.join(outputDir, 'payload-manifest.json'), 'payload manifest')
  for (const [name, expectedFile] of [['archive', 'hermes-backend.tar.gz'], ['installer', installer]]) {
    const asset = manifest[name]
    if (!asset || asset.file !== expectedFile || !Number.isSafeInteger(asset.size) || !/^[0-9a-f]{64}$/.test(asset.sha256)) {
      throw new Error(`payload ${name} metadata is invalid`)
    }
    const filePath = path.join(outputDir, asset.file)
    const stats = assertRegularFile(filePath, `payload ${name}`)
    if (stats.size !== asset.size || sha256File(filePath) !== asset.sha256) {
      throw new Error(`payload ${name} checksum or size mismatch`)
    }
  }
  return true
}

function listFiles(root) {
  const files = []
  const visit = (directory, prefix = '') => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name
      const absolute = path.join(directory, entry.name)
      const stats = fs.lstatSync(absolute)
      if (stats.isSymbolicLink()) throw new Error(`package output cannot be a symbolic link: ${relative}`)
      if (entry.isDirectory()) visit(absolute, relative)
      else if (entry.isFile()) files.push(relative)
      else throw new Error(`package output has unsupported file type: ${relative}`)
    }
  }
  visit(root)
  return files.sort()
}

function expectedBootstrapFiles(bootstrapRoot, installer) {
  const manifest = readJson(path.join(bootstrapRoot, 'auth-toolchain', 'manifest.json'), 'auth toolchain manifest')
  const assetFiles = [manifest.uv, manifest.python, manifest.requirements, ...(manifest.wheels || [])]
    .map(asset => asset?.file)
  if (assetFiles.some(file => typeof file !== 'string' || file.includes('..') || file.includes('\\'))) {
    throw new Error('auth toolchain manifest contains an invalid asset path')
  }
  return new Set([
    installer,
    'hermes-backend.tar.gz',
    'payload-manifest.json',
    'auth-toolchain/manifest.json',
    ...assetFiles.map(file => `auth-toolchain/${file}`)
  ])
}

function rejectUnexpectedFiles(root, allowed) {
  for (const file of listFiles(root)) {
    if (!allowed.has(file)) throw new Error(`unexpected package output: ${file}`)
  }
  for (const file of allowed) {
    assertRegularFile(path.join(root, ...file.split('/')), `declared package output ${file}`)
  }
}

function replaceDirectory(stagingRoot, outputRoot) {
  const backupRoot = `${outputRoot}.package-backup-${process.pid}`
  fs.rmSync(backupRoot, { recursive: true, force: true })
  if (fs.existsSync(outputRoot)) fs.renameSync(outputRoot, backupRoot)
  try {
    fs.renameSync(stagingRoot, outputRoot)
    fs.rmSync(backupRoot, { recursive: true, force: true })
  } catch (error) {
    if (!fs.existsSync(outputRoot) && fs.existsSync(backupRoot)) fs.renameSync(backupRoot, outputRoot)
    throw error
  }
}

function defaultResolveSource({ repoRoot }) {
  const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoRoot, encoding: 'utf8' }).trim()
  const branch = execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], { cwd: repoRoot, encoding: 'utf8' }).trim()
  const dirty = execFileSync('git', ['status', '--porcelain', '--untracked-files=no'], {
    cwd: repoRoot,
    encoding: 'utf8'
  }).trim().length > 0
  return { commit, branch: branch === 'HEAD' ? null : branch, dirty }
}

async function defaultPrepareAuth({ platform, outputDir, inputDir, env }) {
  const preparationEnv = { ...env, HERMES_AUTH_TOOLCHAIN_INPUT_DIR: inputDir }
  const inputs = platform === 'win32'
    ? await prepareWindowsAuthToolchainInputsFromEnvironment(preparationEnv)
    : prepareAuthToolchainInputsFromEnvironment(preparationEnv)
  return buildAuthToolchain({
    outputDir,
    uvPath: inputs.uvPath,
    pythonArchivePath: inputs.pythonArchivePath,
    requirementsPath: inputs.requirementsPath,
    wheelhousePath: inputs.wheelhousePath,
    platform: inputs.platform,
    arch: inputs.arch,
    uvVersion: inputs.uvVersion,
    pythonVersion: inputs.pythonVersion
  })
}

const DEFAULT_DEPENDENCIES = Object.freeze({
  resolveSource: defaultResolveSource,
  buildBackend: ({ repoRoot, stampPath, outputDir, platform, gitRuntimePath, gitRuntimeProvenancePath }) =>
    buildBackendPayload({ repoRoot, stampPath, outputDir, platform, gitRuntimePath, gitRuntimeProvenancePath }),
  prepareAuth: defaultPrepareAuth,
  prepareWindowsGit: prepareWindowsGitRuntime,
  verifyAuth: verifyAuthToolchain,
  verifyPayload,
  verifyGit: auditGitRuntimeArchive
})

export async function preparePackageInputs({
  platform,
  arch,
  repoRoot,
  desktopRoot,
  env = process.env,
  dependencies = {}
}) {
  validateRoots(repoRoot, desktopRoot)
  const target = targetFor(platform, arch)
  const deps = { ...DEFAULT_DEPENDENCIES, ...dependencies }
  const source = deps.resolveSource({ repoRoot, env })
  if (!source || !/^[0-9a-f]{40}$/i.test(source.commit) || /^0+$/.test(source.commit)) {
    throw new Error('package inputs require a real 40-character source commit')
  }
  if (source.dirty !== false) throw new Error('package inputs require a clean tracked working tree')

  const buildRoot = path.join(desktopRoot, 'build')
  const transactionRoot = path.join(buildRoot, `.package-inputs-stage-${process.pid}-${Date.now()}`)
  const bootstrapStage = path.join(transactionRoot, 'bootstrap')
  const authInputStage = path.join(transactionRoot, 'auth-inputs')
  const windowsStage = path.join(transactionRoot, 'windows-prereqs')
  const stampStage = path.join(transactionRoot, 'install-stamp.json')
  fs.rmSync(transactionRoot, { recursive: true, force: true })
  fs.mkdirSync(bootstrapStage, { recursive: true })
  const stamp = {
    schemaVersion: 1,
    commit: source.commit,
    branch: source.branch || null,
    builtAt: new Date().toISOString(),
    dirty: false,
    source: 'local'
  }
  fs.writeFileSync(stampStage, `${JSON.stringify(stamp, null, 2)}\n`)

  try {
    let gitRuntimePath = null
    let gitRuntimeProvenancePath = null
    if (platform === 'win32') {
      fs.mkdirSync(windowsStage, { recursive: true })
      gitRuntimePath = path.join(windowsStage, 'git-bash-runtime.tar.xz')
      const sourcePath = path.resolve(
        env.HERMES_WINDOWS_PORTABLE_GIT_SOURCE ||
          path.join(buildRoot, 'package-input-cache', 'PortableGit-2.55.0.3-64-bit.7z.exe')
      )
      const provenanceRoot = path.join(buildRoot, 'package-input-evidence')
      fs.mkdirSync(provenanceRoot, { recursive: true })
      gitRuntimeProvenancePath = path.join(provenanceRoot, 'git-bash-runtime.provenance.json')
      await deps.prepareWindowsGit({ sourcePath, outputPath: gitRuntimePath, provenancePath: gitRuntimeProvenancePath })
      deps.verifyGit(gitRuntimePath)
      rejectUnexpectedFiles(windowsStage, new Set(['git-bash-runtime.tar.xz']))

      // get-windows publishes its native Windows binding as a GitHub release
      // asset. A user's first-run machine may be offline (and npm removes an
      // optional dependency when that asset cannot be fetched), so carry the
      // pinned package + x64 binding in the Setup payload alongside Git.
      const getWindowsSourcePath = path.resolve(
        env.HERMES_GET_WINDOWS_BUNDLE_SOURCE ||
          path.join(buildRoot, 'package-input-cache', 'get-windows-win32-x64.tar.gz')
      )
      copyWindowsGetWindowsBundle({
        sourcePath: getWindowsSourcePath,
        destinationPath: path.join(bootstrapStage, 'get-windows-win32-x64.tar.gz')
      })
    }

    await deps.buildBackend({
      repoRoot,
      stampPath: stampStage,
      outputDir: bootstrapStage,
      platform,
      gitRuntimePath,
      gitRuntimeProvenancePath
    })
    await deps.prepareAuth({
      platform,
      arch,
      repoRoot,
      desktopRoot,
      outputDir: path.join(bootstrapStage, 'auth-toolchain'),
      inputDir: authInputStage,
      env
    })
    deps.verifyPayload(bootstrapStage, target.installer)
    deps.verifyAuth(path.join(bootstrapStage, 'auth-toolchain'))
    const expected = expectedBootstrapFiles(bootstrapStage, target.installer)
    if (platform === 'win32') expected.add('get-windows-win32-x64.tar.gz')
    rejectUnexpectedFiles(bootstrapStage, expected)

    fs.mkdirSync(buildRoot, { recursive: true })
    replaceDirectory(bootstrapStage, path.join(buildRoot, 'bootstrap'))
    if (platform === 'win32') replaceDirectory(windowsStage, path.join(buildRoot, 'windows-prereqs'))
    const stampFinal = path.join(buildRoot, 'install-stamp.json')
    fs.renameSync(stampStage, `${stampFinal}.tmp-${process.pid}`)
    fs.renameSync(`${stampFinal}.tmp-${process.pid}`, stampFinal)

    return { platform, arch, commit: source.commit, outputs: [...target.outputs] }
  } finally {
    fs.rmSync(transactionRoot, { recursive: true, force: true })
  }
}

function parseCli(argv) {
  const index = argv.indexOf('--platform')
  const platform = index >= 0 ? argv[index + 1] : process.platform
  if (platform !== 'darwin' && platform !== 'win32') throw new Error(`unsupported package platform: ${platform}`)
  return { platform, arch: platform === 'darwin' ? 'arm64' : 'x64' }
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : null
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    const moduleDir = path.dirname(fileURLToPath(import.meta.url))
    const desktopRoot = path.resolve(moduleDir, '..')
    const repoRoot = path.resolve(desktopRoot, '../..')
    const target = parseCli(process.argv.slice(2))
    const result = await preparePackageInputs({ ...target, repoRoot, desktopRoot, env: process.env })
    process.stdout.write(`${JSON.stringify(result)}\n`)
  } catch (error) {
    process.stderr.write(`Hermes package input preparation failed: ${error.message || String(error)}\n`)
    process.exitCode = 1
  }
}
