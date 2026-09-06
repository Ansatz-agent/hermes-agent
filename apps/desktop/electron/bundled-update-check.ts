import { execFile } from 'node:child_process'
import path from 'node:path'

// Use the same read-only checker as `ansatz update --check` so published,
// unpublished, and diverged source identities mean the same thing in both UIs.
export function checkBundledUpdate(root: string, branch: string): Promise<any> {
  const python = path.join(root, 'venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')

  return new Promise((resolve, reject) => {
    execFile(python, ['-m', 'hermes_cli.bundled_update', '--root', root, '--branch', branch], {
      cwd: root,
      encoding: 'utf8',
      timeout: 40_000,
      windowsHide: true,
      env: { ...process.env, PYTHONUTF8: '1' }
    }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(stderr.trim() || error.message))
        return
      }

      try {
        resolve({ ...JSON.parse(stdout), fetchedAt: Date.now() })
      } catch {
        reject(new Error('The installed runtime returned an invalid update-check response.'))
      }
    })
  })
}
