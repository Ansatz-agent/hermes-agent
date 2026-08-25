import fs from 'node:fs'
import path from 'node:path'

type FileExists = (filePath: string) => boolean

/**
 * Resolve the GUI-subsystem Python executable for the desktop auth runtime.
 *
 * Windows virtual-environment `python.exe` files are launcher shims. A
 * detached auth owner started through that console-subsystem shim can re-exec
 * the base interpreter with a visible console even when Electron hid the shim
 * itself. The auth bridge only communicates over explicit stdio pipes and
 * never owns terminal children, so its sibling `pythonw.exe` is the correct
 * executable boundary here. The general Hermes backend deliberately keeps
 * using `python.exe` so its Git/CMD descendants inherit the backend console.
 */
export function resolveNoConsoleAuthPython(
  pythonExecutable: string,
  isWindows: boolean = process.platform === 'win32',
  fileExists: FileExists = fs.existsSync
): string {
  if (!isWindows || !pythonExecutable) {
    return pythonExecutable
  }

  const executableName = path.win32.basename(pythonExecutable).toLowerCase()

  if (executableName === 'pythonw.exe') {
    return pythonExecutable
  }

  if (executableName !== 'python.exe') {
    return pythonExecutable
  }

  const pythonw = path.win32.join(path.win32.dirname(pythonExecutable), 'pythonw.exe')

  return fileExists(pythonw) ? pythonw : pythonExecutable
}
