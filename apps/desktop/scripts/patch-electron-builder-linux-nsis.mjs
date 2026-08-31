import fs from 'node:fs'
import path from 'node:path'

// electron-builder's Linux NSIS path normally launches the just-built
// installer through Wine to extract its uninstaller. That is unnecessary for
// the zlib-compressed NSIS output used here: UninstallerReader is a native JS
// parser and works on Linux without requiring a runnable Wine environment.
// Keep the patch narrowly scoped to Linux and the pinned electron-builder
// implementation in this checkout.
if (process.platform !== 'linux') process.exit(0)

const desktopRoot = path.resolve(import.meta.dirname, '..')
const repoRoot = path.resolve(desktopRoot, '..', '..')
const nsisTargetPath = path.join(repoRoot, 'node_modules', 'app-builder-lib', 'out', 'targets', 'nsis', 'NsisTarget.js')
const marker = 'hermes-linux-nsis-uninstaller-reader'
const needle = `        else {
            const wineVm = new WineVm_1.WineVmManager((_a = packager.config.toolsets) === null || _a === void 0 ? void 0 : _a.wine);
            await wineVm.exec(installerPath, [], { env: { __COMPAT_LAYER: "RunAsInvoker" } });
        }`
const replacement = `        else if (process.platform === "linux") {
            // ${marker}: Linux cross-builds do not need Wine to extract the
            // uninstaller from zlib-compressed NSIS output.
            await nsisUtil_1.UninstallerReader.exec(installerPath, uninstallerPath);
        }
        else {
            const wineVm = new WineVm_1.WineVmManager((_a = packager.config.toolsets) === null || _a === void 0 ? void 0 : _a.wine);
            await wineVm.exec(installerPath, [], { env: { __COMPAT_LAYER: "RunAsInvoker" } });
        }`

if (!fs.existsSync(nsisTargetPath)) {
  console.warn(`[patch-electron-builder] skipped: ${nsisTargetPath} not found`)
  process.exit(0)
}

const source = fs.readFileSync(nsisTargetPath, 'utf8')
if (source.includes(marker)) {
  console.log('[patch-electron-builder] Linux NSIS uninstaller reader already applied')
  process.exit(0)
}

if (!source.includes(needle)) {
  throw new Error('[patch-electron-builder] skipped: expected NsisTarget.js Wine shape not found')
}

fs.writeFileSync(nsisTargetPath, source.replace(needle, replacement))
console.log('[patch-electron-builder] applied Linux NSIS uninstaller reader')
