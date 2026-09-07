import { execFile } from 'node:child_process'

import { beforeEach, expect, test, vi } from 'vitest'

import { checkBundledUpdate } from './bundled-update-check'

vi.mock('node:child_process', () => ({ execFile: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

test('forwards the selected branch to the runtime checker and preserves its status', async () => {
  const status = {
    supported: true,
    updateAvailable: false,
    reason: 'unpublished-source',
    message: 'Publish the commit first.'
  }
  vi.mocked(execFile).mockImplementation((...args: any[]) => {
    args.at(-1)(null, JSON.stringify(status), '')

    return {} as any
  })
  expect(await checkBundledUpdate('/fixture', 'release')).toMatchObject(status)
  expect(vi.mocked(execFile).mock.calls[0][1]).toEqual([
    '-m',
    'hermes_cli.bundled_update',
    '--root',
    '/fixture',
    '--branch',
    'release'
  ])
})

test('reports runtime execution errors instead of hiding their cause', async () => {
  vi.mocked(execFile).mockImplementation((...args: any[]) => {
    args.at(-1)(new Error('exit 1'), '', 'runtime check failed')

    return {} as any
  })
  await expect(checkBundledUpdate('/fixture', 'main')).rejects.toThrow('runtime check failed')
})
