import { ipcMain as electronIpcMain } from 'electron'

export type ChannelAuthPolicy = 'auth-free' | 'local' | 'connection' | 'both'

const AUTH_FREE_CHANNELS_SOURCE = [
  'hermes:auth:status',
  'hermes:auth:login',
  'hermes:auth:logout',
  'hermes:auth-bootstrap:get',
  'hermes:bootstrap:reset',
  'hermes:bootstrap:repair',
  'hermes:bootstrap:continue-local',
  'hermes:bootstrap:cancel',
  'hermes:boot-progress:get',
  'hermes:logs:renderer-error'
] as const

const CONNECTION_CHANNELS = [
  'hermes:connection',
  'hermes:connection:for',
  'hermes:connection:revalidate',
  'hermes:backend:touch',
  'hermes:gateway:ws-url',
  'hermes:gateway:ws-url-for',
  'hermes:window:openSession',
  'hermes:connections:test',
  'hermes:agents:roster',
  'hermes:api'
] as const

const BOTH_CHANNELS = [
  'hermes:window:openInTerminal',
  'hermes:connections:set-primary',
  'hermes:connections:update-all',
  'hermes:connection-config:apply'
] as const

const LOCAL_CHANNELS = [
  'hermes:bootstrap:get',
  'hermes:get-remote-display-reason',
  'hermes:zoom:get',
  'hermes:zoom:set-percent',
  'hermes:titlebar-theme',
  'hermes:native-theme',
  'hermes:translucency',
  'hermes:power-battery:get',
  'hermes:trace:online',
  'hermes:window:openInstance',
  'hermes:wake-indicator:get',
  'hermes:wake-indicator:set',
  'hermes:pet-overlay:open',
  'hermes:pet-overlay:close',
  'hermes:pet-overlay:set-bounds',
  'hermes:pet-overlay:ignore-mouse',
  'hermes:pet-overlay:set-focusable',
  'hermes:pet-overlay:state',
  'hermes:pet-overlay:control',
  'hermes:hud:open',
  'hermes:hud:vibrancy',
  'hermes:hud:ignore-mouse',
  'hermes:hud:move-by',
  'hermes:hud:set-bounds',
  'hermes:hud:session',
  'hermes:hud:close',
  'hermes:connection-config:get',
  'hermes:ssh-config:hosts',
  'hermes:ssh-config:resolve',
  'hermes:connection-config:test',
  'hermes:connections:list',
  'hermes:connections:save',
  'hermes:connections:remove',
  'hermes:connection-config:probe',
  'hermes:connection-config:oauth-login',
  'hermes:connection-config:oauth-logout',
  'hermes:cloud:status',
  'hermes:cloud:login',
  'hermes:cloud:logout',
  'hermes:cloud:discover',
  'hermes:cloud:agent-sign-in',
  'hermes:connection-config:save',
  'hermes:profile:get',
  'hermes:profile:set',
  'hermes:previewShortcutActive',
  'hermes:requestMicrophoneAccess',
  'hermes:window:readBelow',
  'hermes:ambient:claim',
  'hermes:notify',
  'hermes:data-url-read-max:get',
  'hermes:data-url-read-max:set',
  'hermes:readFileDataUrl',
  'hermes:readFileDataUrlForAttach',
  'hermes:readFileText',
  'hermes:selectPaths',
  'hermes:writeClipboard',
  'hermes:selectSavePath',
  'hermes:readClipboard',
  'hermes:saveGatewayFile',
  'hermes:saveImageFromUrl',
  'hermes:saveImageBuffer',
  'hermes:saveClipboardImage',
  'hermes:normalizePreviewTarget',
  'hermes:watchPreviewFile',
  'hermes:watchDirectory',
  'hermes:stopPreviewFileWatch',
  'hermes:active-work',
  'hermes:keep-awake',
  'hermes:quick-entry:settings:get',
  'hermes:quick-entry:settings:set',
  'hermes:quick-entry:submit',
  'hermes:quick-entry:state',
  'hermes:quick-entry:dismiss',
  'hermes:devtools:disable-f12',
  'hermes:openExternal',
  'hermes:find-in-page',
  'hermes:stop-find-in-page',
  'hermes:openPreviewInBrowser',
  'hermes:setting:defaultProjectDir:get',
  'hermes:workspace:sanitize',
  'hermes:setting:defaultProjectDir:set',
  'hermes:setting:defaultProjectDir:pick',
  'hermes:fetchLinkTitle',
  'hermes:logs:reveal',
  'hermes:logs:recent',
  'hermes:fs:readDir',
  'hermes:fs:gitRoot',
  'hermes:fs:reveal',
  'hermes:fs:openDir',
  'hermes:fs:desktopPluginsRoot',
  'hermes:fs:agentPluginsRoot',
  'hermes:fs:rename',
  'hermes:fs:writeText',
  'hermes:fs:trash',
  'hermes:git:worktreeList',
  'hermes:git:worktreeAdd',
  'hermes:git:worktreeRemove',
  'hermes:git:branchSwitch',
  'hermes:git:branchList',
  'hermes:git:baseBranchList',
  'hermes:git:repoStatus',
  'hermes:git:review:list',
  'hermes:git:review:diff',
  'hermes:git:fileDiff',
  'hermes:git:review:stage',
  'hermes:git:review:unstage',
  'hermes:git:review:revert',
  'hermes:git:review:revParse',
  'hermes:git:review:commit',
  'hermes:git:review:commitContext',
  'hermes:git:review:push',
  'hermes:git:review:shipInfo',
  'hermes:git:review:prList',
  'hermes:git:review:fetchPrComment',
  'hermes:git:review:createPr',
  'hermes:git:scanRepos',
  'hermes:terminal:start',
  'hermes:terminal:write',
  'hermes:terminal:resize',
  'hermes:terminal:cwd',
  'hermes:terminal:dispose',
  'hermes:updates:check',
  'hermes:updates:apply',
  'hermes:updates:branch:get',
  'hermes:updates:branch:set',
  'hermes:version',
  'hermes:uninstall:summary',
  'hermes:uninstall:run',
  'hermes:vscode-theme:fetch',
  'hermes:vscode-theme:search',
  'hermes:deep-link-ready'
] as const

const NO_ARGUMENT_CHANNELS = new Set<string>(['hermes:trace:online'])

export const CHANNEL_AUTH_POLICY = buildPolicyMap([
  ['auth-free', AUTH_FREE_CHANNELS_SOURCE],
  ['connection', CONNECTION_CHANNELS],
  ['both', BOTH_CHANNELS],
  ['local', LOCAL_CHANNELS]
])

export const AUTH_FREE_CHANNELS = new Set(
  Object.entries(CHANNEL_AUTH_POLICY)
    .filter(([, policy]) => policy === 'auth-free')
    .map(([channel]) => channel)
)

export type GuardedIpcRequest = {
  channel: string
  policy: ChannelAuthPolicy
  event: any
  args: any[]
}

export type GuardedIpcAuthority = {
  ownsSender: (event: any) => boolean
  resolveConnectionId: (request: GuardedIpcRequest) => string | null
  require: (policy: ChannelAuthPolicy, connectionId: string | null) => unknown | Promise<unknown>
}

type IpcMainLike = {
  handle: (channel: string, listener: (event: any, ...args: any[]) => any) => unknown
  on: (channel: string, listener: (event: any, ...args: any[]) => any) => unknown
}

export class IpcAuthRequiredError extends Error {
  readonly code = 'AUTH_REQUIRED'

  constructor() {
    super('AUTH_REQUIRED')
    this.name = 'IpcAuthRequiredError'
  }
}

export function createGuardedIpc(ipcMain: IpcMainLike, authority: () => GuardedIpcAuthority) {
  const registered = new Set<string>()

  function policyFor(channel: string): ChannelAuthPolicy {
    const policy = CHANNEL_AUTH_POLICY[channel]

    if (!policy) {
      throw new Error(`IPC channel is not classified: ${channel}`)
    }

    return policy
  }

  async function authorize(channel: string, event: any, args: any[]): Promise<void> {
    const policy = policyFor(channel)

    try {
      if (NO_ARGUMENT_CHANNELS.has(channel) && args.length !== 0) {
        throw new Error('unexpected arguments')
      }

      const current = authority()

      if (!current || !current.ownsSender(event)) {
        throw new Error('unknown sender')
      }

      const connectionId = current.resolveConnectionId({ channel, policy, event, args })

      if (policy !== 'auth-free' && (!connectionId || typeof connectionId !== 'string')) {
        throw new Error('missing connection id')
      }

      await current.require(policy, connectionId)
    } catch {
      throw new IpcAuthRequiredError()
    }
  }

  function record(channel: string) {
    policyFor(channel)

    if (registered.has(channel)) {
      throw new Error(`IPC channel is registered more than once: ${channel}`)
    }

    registered.add(channel)
  }

  function handle(channel: string, listener: (event: any, ...args: any[]) => any) {
    record(channel)
    ipcMain.handle(channel, async (event, ...args) => {
      await authorize(channel, event, args)

      return listener(event, ...args)
    })
  }

  function on(channel: string, listener: (event: any, ...args: any[]) => any) {
    record(channel)
    ipcMain.on(channel, (event, ...args) => {
      void (async () => {
        try {
          await authorize(channel, event, args)
        } catch {
          // send()-style IPC has no rejection channel. Keep the denial bounded
          // and machine-readable without copying bridge/keyring error text.
          event.returnValue = { error: { code: 'AUTH_REQUIRED' } }

          return
        }

        listener(event, ...args)
      })()
    })
  }

  function assertCoverage(options: { allowUnregistered?: Iterable<string> } = {}) {
    const allowed = new Set(options.allowUnregistered ?? [])

    const missing = Object.keys(CHANNEL_AUTH_POLICY).filter(
      channel => !registered.has(channel) && !allowed.has(channel)
    )

    if (missing.length > 0) {
      throw new Error(`Missing guarded IPC registrations: ${missing.join(', ')}`)
    }
  }

  return { assertCoverage, handle, on, registered }
}

let currentAuthority: GuardedIpcAuthority = {
  ownsSender: () => false,
  resolveConnectionId: () => null,
  require: () => {
    throw new IpcAuthRequiredError()
  }
}

const productionGuard = createGuardedIpc(electronIpcMain as unknown as IpcMainLike, () => currentAuthority)

export function configureGuardedIpcAuthority(authority: GuardedIpcAuthority) {
  currentAuthority = authority
}

export const guardedHandle = productionGuard.handle
export const guardedOn = productionGuard.on
export const assertGuardedIpcCoverage = productionGuard.assertCoverage

function buildPolicyMap(
  groups: ReadonlyArray<readonly [ChannelAuthPolicy, readonly string[]]>
): Readonly<Record<string, ChannelAuthPolicy>> {
  const entries: Array<[string, ChannelAuthPolicy]> = []
  const seen = new Set<string>()

  for (const [policy, channels] of groups) {
    for (const channel of channels) {
      if (seen.has(channel)) {
        throw new Error(`IPC channel has multiple auth policies: ${channel}`)
      }

      seen.add(channel)
      entries.push([channel, policy])
    }
  }

  return Object.freeze(Object.fromEntries(entries))
}
