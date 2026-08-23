import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { spawnSync } from "node:child_process"

export const RESTRICTED_DMG_VOLUME_ERROR =
  "ditto: /Volumes/Install Ansatz Voice Trace Client/Ansatz Voice Trace Client.app: Operation not permitted"

function hasMacTarget(args) {
  return args.some((arg) => arg === "--mac" || arg.startsWith("--mac="))
}

function hasDmgTarget(args) {
  return args.some((arg) => arg === "dmg" || arg === "--mac=dmg")
}

function hasExplicitIdentity(args) {
  return args.some(
    (arg) =>
      arg === "--config.mac.identity" ||
      arg.startsWith("--config.mac.identity=") ||
      arg === "-c.mac.identity" ||
      arg.startsWith("-c.mac.identity="),
  )
}

export function ensureMacSigningIdentity(
  args,
  { platform = process.platform, env = process.env } = {},
) {
  if (
    platform !== "darwin" ||
    !hasMacTarget(args) ||
    hasExplicitIdentity(args) ||
    env.CSC_IDENTITY_AUTO_DISCOVERY !== "false" ||
    env.CSC_LINK ||
    env.CSC_NAME
  ) {
    return [...args]
  }
  return [...args, "--config.mac.identity=-"]
}

export function isMacDmgRequest(args, platform = process.platform) {
  return platform === "darwin" && hasMacTarget(args) && hasDmgTarget(args)
}

export function shouldUseRestrictedVolumeFallback({
  args,
  platform = process.platform,
  status,
  output,
  packagedAppExists,
}) {
  return (
    status !== 0 &&
    isMacDmgRequest(args, platform) &&
    packagedAppExists &&
    output.includes(RESTRICTED_DMG_VOLUME_ERROR)
  )
}

function defaultRunCommand(command, args) {
  const result = spawnSync(command, args, { encoding: "utf8" })
  if (result.stdout) process.stdout.write(result.stdout)
  if (result.stderr) process.stderr.write(result.stderr)
  return result
}

function requireSuccessfulCommand(result, label) {
  if (result.error) {
    throw new Error(`${label} failed to start: ${result.error.message}`)
  }
  if (result.status !== 0) {
    throw new Error(`${label} failed with exit code ${result.status ?? "unknown"}`)
  }
}

function defaultAppSizeKib(packagedApp) {
  const result = spawnSync("du", ["-sk", packagedApp], { encoding: "utf8" })
  requireSuccessfulCommand(result, "measure packaged app")
  const size = Number.parseInt(result.stdout.trim().split(/\s+/, 1)[0], 10)
  if (!Number.isSafeInteger(size) || size <= 0) {
    throw new Error("measure packaged app returned an invalid size")
  }
  return size
}

export function buildRestrictedVolumeDmg({
  packagedApp,
  dmgPath,
  volumeName = "Install Ansatz Voice Trace Client",
  runCommand = defaultRunCommand,
  appSizeKib = defaultAppSizeKib,
  makeTemporaryDirectory = () =>
    fs.mkdtempSync(path.join(os.tmpdir(), "ansatz-voice-trace-dmg-fallback-")),
}) {
  const temporaryRoot = makeTemporaryDirectory()
  const readWriteDmg = path.join(temporaryRoot, "Ansatz-Voice-Trace-Client-rw.dmg")
  const mountPoint = path.join(temporaryRoot, "mount")
  let attached = false
  let safeToRemove = true

  fs.mkdirSync(mountPoint)
  const imageMib = Math.ceil(appSizeKib(packagedApp) / 1024) + 128

  const run = (command, args, label) => {
    const result = runCommand(command, args)
    requireSuccessfulCommand(result, label)
  }

  try {
    run(
      "hdiutil",
      ["create", "-size", `${imageMib}m`, "-fs", "HFS+", "-volname", volumeName, "-type", "UDIF", readWriteDmg],
      "create fallback DMG",
    )
    run(
      "hdiutil",
      ["attach", "-nobrowse", "-mountpoint", mountPoint, readWriteDmg],
      "attach fallback DMG",
    )
    attached = true
    run(
      "/usr/bin/ditto",
      [packagedApp, path.join(mountPoint, "Ansatz Voice Trace Client.app")],
      "copy app into fallback DMG",
    )
    run("ln", ["-s", "/Applications", path.join(mountPoint, "Applications")], "link Applications in fallback DMG")
    run("hdiutil", ["detach", mountPoint], "detach fallback DMG")
    attached = false
    run(
      "hdiutil",
      ["convert", readWriteDmg, "-format", "UDZO", "-ov", "-o", dmgPath],
      "compress fallback DMG",
    )
    run("hdiutil", ["verify", dmgPath], "verify fallback DMG")
    return dmgPath
  } finally {
    if (attached) {
      const detach = runCommand("hdiutil", ["detach", mountPoint])
      safeToRemove = !detach.error && detach.status === 0
    }
    if (safeToRemove) {
      fs.rmSync(temporaryRoot, { recursive: true, force: true })
    } else {
      console.error(`[run-electron-builder] retained mounted fallback workspace: ${temporaryRoot}`)
    }
  }
}
