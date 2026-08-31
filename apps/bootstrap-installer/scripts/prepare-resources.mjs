import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const installerRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = path.resolve(installerRoot, '../..')
const desktopBuild = path.join(repoRoot, 'apps', 'desktop', 'build')
const resourceRoot = path.join(installerRoot, 'src-tauri', 'resources', 'bootstrap')
const requestedPlatform = process.argv.includes('--platform')
  ? process.argv[process.argv.indexOf('--platform') + 1]
  // Tauri sets TAURI_ENV_PLATFORM for beforeBuildCommand hooks based on the
  // target triple. Prefer it over Node's host platform so a Linux/macOS
  // cross-build stages the target's installer resources.
  : process.env.TAURI_ENV_PLATFORM || process.platform
// Tauri names the Windows target `windows`, while the package scripts and
// resource filenames use Node's `win32` spelling.
const platform = requestedPlatform === 'windows' ? 'win32' : requestedPlatform
const installer = platform === 'win32' ? 'install.ps1' : 'install.sh'
const gitRuntimeSource = path.join(desktopBuild, 'windows-prereqs', 'git-bash-runtime.tar.xz')
const getWindowsBundleSource = path.join(desktopBuild, 'bootstrap', 'get-windows-win32-x64.tar.gz')

if (platform !== 'win32' && platform !== 'darwin') {
  throw new Error(`unsupported Setup resource platform: ${platform}`)
}

const required = [
  path.join(desktopBuild, 'bootstrap', installer),
  path.join(desktopBuild, 'bootstrap', 'hermes-backend.tar.gz'),
  path.join(desktopBuild, 'bootstrap', 'payload-manifest.json'),
  path.join(desktopBuild, 'bootstrap', 'auth-toolchain', 'manifest.json')
]
if (platform === 'win32') {
  required.push(gitRuntimeSource)
  required.push(getWindowsBundleSource)
}

for (const file of required) {
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) {
    throw new Error(
      `missing generated Setup resource ${file}; run ` +
      `npm run prepare:package:${platform === 'win32' ? 'win' : 'mac'} --workspace apps/desktop first`
    )
  }
}

fs.rmSync(resourceRoot, { recursive: true, force: true })
fs.mkdirSync(resourceRoot, { recursive: true })

const copies = [
  [required[0], path.join(resourceRoot, installer)],
  [required[1], path.join(resourceRoot, 'hermes-backend.tar.gz')],
  [required[2], path.join(resourceRoot, 'payload-manifest.json')]
]
if (platform === 'win32') {
  copies.push([gitRuntimeSource, path.join(resourceRoot, 'git-bash-runtime.tar.xz')])
  copies.push([getWindowsBundleSource, path.join(resourceRoot, 'get-windows-win32-x64.tar.gz')])
}

for (const [source, destination] of copies) {
  fs.copyFileSync(source, destination)
}

// The authentication runtime is part of the release payload too.  It carries
// the exact Windows uv/Python/wheel inputs, so the first-run installer does
// not need to download or resolve a host Python installation.
fs.cpSync(
  path.join(desktopBuild, 'bootstrap', 'auth-toolchain'),
  path.join(resourceRoot, 'auth-toolchain'),
  { recursive: true, dereference: false }
)

process.stdout.write(`[prepare-setup-resources] staged ${copies.length + 1} entries for ${platform}\n`)
