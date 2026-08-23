// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from "node:fs"
import path from "node:path"
import { spawn, spawnSync } from "node:child_process"
import { createRequire } from "node:module"

import {
  buildRestrictedVolumeDmg,
  ensureMacSigningIdentity,
  isMacDmgRequest,
  shouldUseRestrictedVolumeFallback,
} from "./macos-dmg-builder.mjs"

const require = createRequire(import.meta.url)

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve("electron/package.json")), "dist")
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === "darwin") {
    return path.join(dist, "Electron.app", "Contents", "MacOS", "Electron")
  }
  if (process.platform === "win32") {
    return path.join(dist, "electron.exe")
  }
  return path.join(dist, "electron")
}

function electronBuilderCli() {
  const pkgJson = require.resolve("electron-builder/package.json")
  const bin = require(pkgJson).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(path.dirname(pkgJson), rel)
}

function runBuilder(command, args, captureOutput) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [command, ...args], {
      stdio: captureOutput ? ["inherit", "pipe", "pipe"] : "inherit",
    })
    let output = ""
    const remember = (chunk, destination) => {
      destination.write(chunk)
      output = `${output}${chunk.toString("utf8")}`.slice(-2_000_000)
    }
    if (captureOutput) {
      child.stdout.on("data", (chunk) => remember(chunk, process.stdout))
      child.stderr.on("data", (chunk) => remember(chunk, process.stderr))
    }
    child.once("error", (error) => resolve({ status: null, error, output }))
    child.once("close", (status) => resolve({ status, error: null, output }))
  })
}

function runCheck(command, args, label) {
  const result = spawnSync(command, args, { stdio: "inherit" })
  if (result.error) {
    console.error(`[run-electron-builder] ${label} failed to start: ${result.error.message}`)
    return false
  }
  if (result.status !== 0) {
    console.error(`[run-electron-builder] ${label} failed with exit code ${result.status ?? "unknown"}`)
    return false
  }
  return true
}

function macPackagePaths() {
  const desktopRoot = path.resolve(import.meta.dirname, "..")
  const desktopPackage = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"))
  const arch = process.arch
  const appDirectory = arch === "x64" ? "mac" : `mac-${arch}`
  const releaseDirectory = path.join(desktopRoot, "release")
  return {
    packagedApp: path.join(releaseDirectory, appDirectory, `${desktopPackage.productName}.app`),
    dmgPath: path.join(
      releaseDirectory,
      desktopPackage.build.artifactName
        .replace("${version}", desktopPackage.version)
        .replace("${os}", "mac")
        .replace("${arch}", arch)
        .replace("${ext}", "dmg"),
    ),
  }
}

const dist = electronDistDir()
const args = []
if (dist && fs.existsSync(distBinary(dist))) {
  args.push(`-c.electronDist=${dist}`)
} else {
  console.warn(
    "[run-electron-builder] no local electron dist; electron-builder will fetch " +
      "via @electron/get (electronVersion + ELECTRON_MIRROR)."
  )
}
const requestedArgs = ensureMacSigningIdentity(process.argv.slice(2))
args.push(...requestedArgs)

const macDmgRequested = isMacDmgRequest(requestedArgs)
const result = await runBuilder(electronBuilderCli(), args, macDmgRequested)

if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}

if (!macDmgRequested) {
  process.exit(result.status == null ? 1 : result.status)
}

const { packagedApp, dmgPath } = macPackagePaths()
const useFallback = shouldUseRestrictedVolumeFallback({
  args: requestedArgs,
  status: result.status,
  output: result.output,
  packagedAppExists: fs.existsSync(packagedApp),
})

if (result.status !== 0 && !useFallback) {
  process.exit(result.status == null ? 1 : result.status)
}

if (!runCheck("codesign", ["--verify", "--deep", "--strict", packagedApp], "packaged app signature verification")) {
  process.exit(1)
}

if (useFallback) {
  console.warn(
    "[run-electron-builder] mounted DMG volume denied by macOS; " +
      "using the verified restricted-volume fallback.",
  )
  try {
    buildRestrictedVolumeDmg({ packagedApp, dmgPath })
  } catch (error) {
    console.error(
      `[run-electron-builder] restricted-volume fallback failed: ${error instanceof Error ? error.message : String(error)}`,
    )
    process.exit(1)
  }
} else if (!fs.existsSync(dmgPath)) {
  console.error(`[run-electron-builder] expected DMG was not produced: ${dmgPath}`)
  process.exit(1)
} else if (!runCheck("hdiutil", ["verify", dmgPath], "DMG verification")) {
  process.exit(1)
}

console.log(`[run-electron-builder] verified DMG: ${dmgPath}`)
process.exit(0)
