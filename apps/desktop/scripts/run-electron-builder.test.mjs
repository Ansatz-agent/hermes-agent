import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

import { test } from "vitest"

import {
  RESTRICTED_DMG_VOLUME_ERROR,
  buildRestrictedVolumeDmg,
  ensureMacSigningIdentity,
  packagedMacAppName,
  shouldUseRestrictedVolumeFallback,
} from "./macos-dmg-builder.mjs"

test("packaged macOS app path follows electron-builder executableName", () => {
  const desktopPackage = JSON.parse(
    fs.readFileSync(new URL("../package.json", import.meta.url), "utf8"),
  )

  assert.equal(packagedMacAppName(desktopPackage), "AnsatzVoiceTraceClient.app")
})

test("macOS package builds use explicit ad-hoc signing when identity discovery is disabled", () => {
  assert.deepEqual(
    ensureMacSigningIdentity(["--mac", "dmg"], {
      platform: "darwin",
      env: { CSC_IDENTITY_AUTO_DISCOVERY: "false" },
    }),
    ["--mac", "dmg", "--config.mac.identity=-"],
  )

  assert.deepEqual(
    ensureMacSigningIdentity(["--mac", "dmg", "--config.mac.identity=Developer ID Application: Ansatz Agent"], {
      platform: "darwin",
      env: { CSC_IDENTITY_AUTO_DISCOVERY: "false" },
    }),
    ["--mac", "dmg", "--config.mac.identity=Developer ID Application: Ansatz Agent"],
  )

  assert.deepEqual(
    ensureMacSigningIdentity(["--mac", "dmg"], {
      platform: "darwin",
      env: {
        CSC_IDENTITY_AUTO_DISCOVERY: "false",
        CSC_LINK: "encrypted-signing-certificate",
      },
    }),
    ["--mac", "dmg"],
  )

  assert.deepEqual(
    ensureMacSigningIdentity(["--win", "nsis"], {
      platform: "win32",
      env: { CSC_IDENTITY_AUTO_DISCOVERY: "false" },
    }),
    ["--win", "nsis"],
  )
})

test("restricted-volume fallback is limited to the exact macOS DMG failure", () => {
  const request = {
    args: ["--mac", "dmg"],
    platform: "darwin",
    status: 1,
    output: `builder failed\n${RESTRICTED_DMG_VOLUME_ERROR}\n`,
    packagedAppExists: true,
  }
  assert.equal(shouldUseRestrictedVolumeFallback(request), true)
  assert.equal(shouldUseRestrictedVolumeFallback({ ...request, args: ["--mac", "zip"] }), false)
  assert.equal(shouldUseRestrictedVolumeFallback({ ...request, packagedAppExists: false }), false)
  assert.equal(shouldUseRestrictedVolumeFallback({ ...request, output: "unrelated builder failure" }), false)
  assert.equal(shouldUseRestrictedVolumeFallback({ ...request, status: 0 }), false)
})

test("restricted-volume fallback creates, verifies, and cleans a compressed DMG", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ansatz-direct-dmg-test-"))
  const packagedApp = path.join(root, "release", "mac-arm64", "Ansatz Voice Trace Client.app")
  const dmgPath = path.join(root, "release", "Ansatz-Voice-Trace-Client-0.17.0-mac-arm64.dmg")
  const temporaryRoot = path.join(root, "fallback")
  fs.mkdirSync(packagedApp, { recursive: true })
  const calls = []

  try {
    buildRestrictedVolumeDmg({
      packagedApp,
      dmgPath,
      makeTemporaryDirectory: () => {
        fs.mkdirSync(temporaryRoot)
        return temporaryRoot
      },
      appSizeKib: () => 2048,
      runCommand: (command, args) => {
        calls.push([command, ...args])
        return { status: 0, stdout: "", stderr: "" }
      },
    })

    assert.deepEqual(calls, [
      ["hdiutil", "create", "-size", "130m", "-fs", "HFS+", "-volname", "Install Ansatz Voice Trace Client", "-type", "UDIF", path.join(temporaryRoot, "Ansatz-Voice-Trace-Client-rw.dmg")],
      ["hdiutil", "attach", "-nobrowse", "-mountpoint", path.join(temporaryRoot, "mount"), path.join(temporaryRoot, "Ansatz-Voice-Trace-Client-rw.dmg")],
      ["/usr/bin/ditto", packagedApp, path.join(temporaryRoot, "mount", "Ansatz Voice Trace Client.app")],
      ["ln", "-s", "/Applications", path.join(temporaryRoot, "mount", "Applications")],
      ["hdiutil", "detach", path.join(temporaryRoot, "mount")],
      ["hdiutil", "convert", path.join(temporaryRoot, "Ansatz-Voice-Trace-Client-rw.dmg"), "-format", "UDZO", "-ov", "-o", dmgPath],
      ["hdiutil", "verify", dmgPath],
    ])
    assert.equal(fs.existsSync(temporaryRoot), false)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
