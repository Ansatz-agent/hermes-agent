# Desktop Trace Continuous Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让可信本地账户的 Trace 在 Trace 服务、网络或 auth bridge 暂时不可达时仍持续加密落盘，并在恢复后无感补传。

**Architecture:** 保留现有 `TraceForwarder` 的 OTLP 验证、durable-local/cloud-receipt 竞速、outbox 和 uploader 能力，但用 `TraceDurabilityRuntime` 将其生命周期改为账户级稳定 session。同账户 Session/token 更新只 rebind owner 和 credential source；不停 listener/store，不 detach façade；只有 logout、权威撤销或账户切换执行硬停止。

**Tech Stack:** Electron 40、Node.js 26.7.0、TypeScript 6、Vitest 4、Node HTTP、Electron `safeStorage`、加密 Trace outbox journal、OTLP/HTTP protobuf。

---

## 执行约束

- 权威设计是 `docs/superpowers/specs/2026-08-27-desktop-trace-continuous-durability-design.md`。
- 本计划只修改 Desktop 客户端；不修改 Trace ingest 服务、receipt 或账户服务 API。
- 当前没有 Developer ID 证书；本计划不开发正式签名、notarization、CI 签名凭据或
  release 门槛。macOS 只做 ad-hoc 开发 DMG 的安装验收。
- 每个生产行为任务严格按 red → green → focused regression → commit 执行；纯集成/打包验证任务必须在前置 red/green 任务通过后保持 green。
- 每个任务完成后使用当地 diff、类型检查和测试自审；不依赖 Claude 审核。
- 使用 `.node-version` 的 Node 26.7.0。所有 Desktop 命令从仓库根目录以 `npm run --workspace apps/desktop ...` 运行。
- 测试必须调用真实导出行为；不新增读取 `.ts`/`.tsx` 源码文本的正则断言。被本修复触及的旧源码文本测试要换成行为测试。
- 不将 bearer、Trace credential、Cookie、data/wrapped key、完整 account/session id 或 OTLP payload 写入日志。
- 保留 2 GiB 容量、30 天保留、receipt tombstone、迁移、quarantine、compaction、去重和幂等合同。

## 文件映射

### 新文件

- `apps/desktop/electron/trace-durability-runtime.ts`：账户级 Trace session 状态机、single-flight activate、同账户 rebind、硬 lock 和诊断转换。
- `apps/desktop/electron/trace-durability-runtime.test.ts`：不依赖 Electron 全局的真实状态机行为测试。

### 修改文件

- `apps/desktop/electron/trace-credential-provider.ts` / `.test.ts`：可 rebind 且会拒绝过期 in-flight 结果的 credential source。
- `apps/desktop/electron/trace-forwarder.ts` / `.test.ts`：同账户 owner rebind、upload authorization generation 和异步终止撤销确认。
- `apps/desktop/electron/trace-recovery-controller.ts` / `.test.ts`：增加 `owner-rebound` 恢复信号，确保 rebind 立即用新凭据 pump。
- `apps/desktop/electron/main.ts`：将全局 Trace forwarder/store/lifecycle/startup 状态收敛到 runtime，façade 只在硬锁定时 detach。
- `apps/desktop/electron/trace-forwarder.test.ts`、`apps/desktop/electron/desktop-runtime-gate.test.ts`：移除本次路径的源码文本匹配，改由 runtime/integration 行为覆盖。
- `apps/desktop/electron/trace-continuity.integration.test.ts`：加入 auth bridge/Trace 不可达、同账户 Session 更新和跨重启补传。

### Task 1: 增加可无感 rebind 的 Trace credential source

**Files:**
- Modify: `apps/desktop/electron/trace-credential-provider.ts:1-135`
- Modify: `apps/desktop/electron/trace-credential-provider.test.ts`

- [ ] **Step 1: 先写过期 credential flight 失败测试**

在 `trace-credential-provider.test.ts` 增加：

```ts
function validOwner(overrides: Partial<TraceOwner> = {}): TraceOwner {
  const accountId = overrides.accountId ?? '11111111-1111-4111-8111-111111111111'
  return {
    accountId,
    accountKey: `account-${accountId}`,
    installationId,
    sessionId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    ...overrides
  }
}

test('a same-account rebind rejects the old loader result and only caches the new binding', async () => {
  const oldFlight = deferred<TraceCredential>()
  const ownerA = validOwner({ sessionId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' })
  const ownerB = validOwner({ sessionId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' })
  const source = new RebindableTraceCredentialSource()
  source.bind(ownerA, () => oldFlight.promise)
  const provider = new RefreshingTraceCredentialProvider(source, { clock: () => now, installationId })

  const stale = provider.current()
  source.bind(ownerB, async () => credential('new-session-token-1234567890'))
  provider.invalidate()
  oldFlight.resolve(credential('old-session-token-1234567890'))

  await assert.rejects(stale, /trace_credential_binding_changed/)
  assert.equal((await provider.current()).access_token, 'new-session-token-1234567890')
  assert.deepEqual(source.owner(), ownerB)
})

test('credential source refuses cross-account rebind and keeps the active binding', async () => {
  const source = new RebindableTraceCredentialSource()
  const ownerA = validOwner()
  source.bind(ownerA, async () => credential('account-a-token-1234567890'))

  assert.throws(
    () => source.bind(validOwner({ accountId: '22222222-2222-4222-8222-222222222222' }), async () => credential('x')),
    /trace_credential_account_mismatch/
  )
  assert.deepEqual(source.owner(), ownerA)
})
```

在测试 helper 中加入一个返回合法 `TraceOwner` 的 `validOwner(overrides)`，使用现有 canonical UUID 格式。

- [ ] **Step 2: 运行测试确认 red**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-credential-provider.test.ts
```

Expected: FAIL，`RebindableTraceCredentialSource` 未导出。

- [ ] **Step 3: 实现可 rebind source**

在 `trace-credential-provider.ts` 增加：

```ts
import { type TraceOwner, validateTraceOwner } from './trace-outbox-types'

type BoundTraceCredentialLoader = (forceRefresh: boolean) => Promise<TraceCredential>

type TraceCredentialBinding = {
  generation: number
  loader: BoundTraceCredentialLoader
  owner: TraceOwner
}

export class RebindableTraceCredentialSource implements TraceCredentialSource {
  private binding: TraceCredentialBinding | null = null
  private generation = 0

  bind(owner: TraceOwner, loader: BoundTraceCredentialLoader): number {
    const next = validateTraceOwner(owner).owner
    const current = this.binding?.owner

    if (
      current &&
      (current.accountKey !== next.accountKey || current.installationId !== next.installationId)
    ) {
      throw new Error('trace_credential_account_mismatch')
    }

    this.generation += 1
    this.binding = { generation: this.generation, loader, owner: { ...next } }
    return this.generation
  }

  clear(): void {
    this.generation += 1
    this.binding = null
  }

  owner(): TraceOwner | null {
    return this.binding ? { ...this.binding.owner } : null
  }

  async load(forceRefresh: boolean): Promise<TraceCredential> {
    const binding = this.binding
    if (binding === null) throw new Error('trace_credential_binding_unavailable')

    const credential = await binding.loader(forceRefresh)
    if (this.binding !== binding || binding.generation !== this.generation) {
      throw new Error('trace_credential_binding_changed')
    }
    return credential
  }
}
```

- [ ] **Step 4: 运行 provider 全文件测试并提交**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-credential-provider.test.ts
git add apps/desktop/electron/trace-credential-provider.ts apps/desktop/electron/trace-credential-provider.test.ts
git commit -m "feat(desktop): rebind trace credential source"
```

Expected: provider 文件全部 PASS；错误文本不包含 token。

### Task 2: 让 TraceForwarder 在同账户 Session 变化时保持 ingress

**Files:**
- Modify: `apps/desktop/electron/trace-forwarder.ts:50-540`
- Modify: `apps/desktop/electron/trace-forwarder.test.ts`
- Modify: `apps/desktop/electron/trace-recovery-controller.ts:4-14`
- Modify: `apps/desktop/electron/trace-recovery-controller.test.ts`

- [ ] **Step 1: 先写 listener/bearer/store 不变的失败测试**

在 `trace-forwarder.test.ts` 用真实临时 store 增加：

```ts
test('same-account owner rebind keeps ingress and durably records the new session owner', async () => {
  const { root, store } = await temporaryStore()
  const provider = new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow })
  const forwarder = new TraceForwarder({
    credentialProvider: provider,
    fetchImpl: async () => { throw new Error('offline') },
    installationId,
    store
  })
  const firstOwner = validOwner()
  const nextOwner = { ...firstOwner, sessionId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' }
  const started = await forwarder.start(firstOwner)

  try {
    forwarder.rebindOwner(nextOwner)
    assert.deepEqual(forwarder.ingress(), started)
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    const batch = await store.peekEligible(Date.now())
    assert.equal(batch?.owner.sessionId, nextOwner.sessionId)
  } finally {
    await forwarder.stop({ flushMs: 0 })
    await rm(root, { recursive: true, force: true })
  }
})

test('cross-account rebind is rejected without changing the active ingress', async () => {
  const { root, store } = await temporaryStore()
  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource()),
    installationId,
    store
  })
  const started = await forwarder.start(validOwner())
  const other = validOwner({ accountId: '22222222-2222-4222-8222-222222222222' })

  assert.throws(() => forwarder.rebindOwner(other), /trace_owner_account_mismatch/)
  assert.deepEqual(forwarder.ingress(), started)
  await forwarder.stop({ flushMs: 0 })
  await rm(root, { recursive: true, force: true })
})
```

- [ ] **Step 2: 写 stale 403 不能锁定新 Session 的失败测试**

使用 deferred fetch：旧 Session 上传发出后 rebind，再释放一个携带旧 Session 撤销 payload 的 403。

```ts
assert.deepEqual(revocations, [])
assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
```

随后让新 generation 获得与新 Session 匹配的 403，`onTerminalRevocation` 返回 `true`，断言只记录新撤销且 pump 停止。

- [ ] **Step 3: 运行聚焦测试确认 red**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-forwarder.test.ts
```

Expected: FAIL，`rebindOwner()`/`ingress()` 不存在，旧 batch owner 会导致撤销语义错误。

- [ ] **Step 4: 实现 owner 与 upload authorization generation**

在 `TraceForwarder` 中增加 `authorizationGeneration`，保留 `generation` 作为 listener/account 硬生命周期：

```ts
private authorizationGeneration = 0

ingress(): { endpoint: string; localBearer: string } | null {
  const address = this.server?.address()
  return address && typeof address !== 'string' && this.localBearer
    ? { endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: this.localBearer }
    : null
}

rebindOwner(owner: TraceOwner): void {
  const next = validateTraceOwner(owner).owner
  const current = this.owner
  if (!this.admissionOpen || current === null) throw new Error('trace_forwarder_unavailable')
  if (current.accountKey !== next.accountKey || current.installationId !== next.installationId) {
    throw new Error('trace_owner_account_mismatch')
  }
  this.authorizationGeneration += 1
  this.owner = { ...next }
  this.credentialProvider.invalidate()
  this.retryByBatch.clear()
  this.recovery.trigger('owner-rebound')
}
```

在 `start()`/`stop()` 中提升 `authorizationGeneration`。`sendForReceipt()` 在读 credential 前捕获
`authorizationOwner + authorizationGeneration`，在 fetch 前后、receipt parse 后都调用：

```ts
private requireUploadAuthorization(owner: TraceOwner, generation: number): void {
  if (
    generation !== this.authorizationGeneration ||
    this.owner?.accountKey !== owner.accountKey ||
    this.owner?.sessionId !== owner.sessionId
  ) {
    throw new UpstreamFailure(null)
  }
}
```

将 callback contract 改为：

```ts
onTerminalRevocation?: (revocation: TerminalTraceRevocation) => boolean | Promise<boolean>
```

只有 callback 返回 `true` 才设置 `terminalRevoked = true`；过期 authorization generation 的 403 必须在 callback 前被丢弃。同时把 `owner-rebound` 加入 `TraceRecoveryReason` 和 `ALL_TRACE_RECOVERY_REASONS`。

- [ ] **Step 5: 运行 forwarder/recovery 回归并提交**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-forwarder.test.ts electron/trace-recovery-controller.test.ts
git add apps/desktop/electron/trace-forwarder.ts apps/desktop/electron/trace-forwarder.test.ts apps/desktop/electron/trace-recovery-controller.ts apps/desktop/electron/trace-recovery-controller.test.ts
git commit -m "fix(desktop): keep trace ingress across owner refresh"
```

Expected: 新测试和已有 HTTP、receipt、retry、shutdown 测试全部 PASS。

### Task 3: 建立账户级 TraceDurabilityRuntime

**Files:**
- Create: `apps/desktop/electron/trace-durability-runtime.ts`
- Create: `apps/desktop/electron/trace-durability-runtime.test.ts`

- [ ] **Step 1: 先写 runtime 状态机失败测试**

测试创建一个真实 `TraceDurabilityRuntime` 和记录调用的 fake session，覆盖：

```ts
const first = await runtime.activate({ owner: ownerA, scope }, createSession)
const second = await runtime.activate({ owner: ownerB, scope }, createSession)

assert.equal(first.kind, 'created')
assert.equal(second.kind, 'rebound')
assert.equal(createCalls, 1)
assert.equal(stopCalls, 0)
assert.deepEqual(reboundOwners, [ownerB])
assert.deepEqual(runtime.current()?.ingress, first.context.ingress)
```

再添加：100 个同账户并发 activate 只 create 一次；不同 account key 的 activate 抛出
`trace_account_switch_requires_lock`；`lock()` 提升 generation，等待 session stop，清空 current；过期 create 结果立即 stop 且不发布。

- [ ] **Step 2: 运行测试确认 red**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-durability-runtime.test.ts
```

Expected: FAIL，新模块不存在。

- [ ] **Step 3: 实现窄 runtime contract**

`trace-durability-runtime.ts` 使用以下公开类型：

```ts
export type TraceDurabilityContext = {
  ingress: TraceIngressEndpoint
  owner: TraceOwner
  scope: ConnectionScope
}

export type TraceDurabilitySession = {
  compactIfIdle(): Promise<boolean>
  context(): TraceDurabilityContext
  rebind(owner: TraceOwner, scope: ConnectionScope): void
  stop(flushMs: number): Promise<void>
  trigger(reason: TraceRecoveryReason): void
}

export type TraceActivation = {
  context: TraceDurabilityContext
  kind: 'created' | 'rebound' | 'reused'
}

export type TraceDurabilityDiagnostic = {
  code:
    | 'trace_admission_ready'
    | 'trace_owner_rebound'
    | 'trace_upload_degraded'
    | 'trace_upload_recovered'
    | 'trace_storage_failed'
    | 'trace_terminal_locked'
    | 'trace_backlog_recovered'
  errorClass?: string
  pending?: number
  pendingBytes?: number
  retryAttempt?: number
}

export class TraceDurabilityRuntime {
  private active: TraceDurabilitySession | null = null
  private flight: Promise<TraceDurabilitySession> | null = null
  private generation = 0

  constructor(private readonly diagnostic: (event: TraceDurabilityDiagnostic) => void = () => {}) {}

  current(): TraceDurabilityContext | null {
    const value = this.active?.context()
    return value ? { ...value, ingress: { ...value.ingress }, owner: { ...value.owner }, scope: { ...value.scope } } : null
  }

  async activate(
    requested: { owner: TraceOwner; scope: ConnectionScope },
    create: () => Promise<TraceDurabilitySession>
  ): Promise<TraceActivation> {
    const owner = validateTraceOwner(requested.owner).owner
    const current = this.active?.context()
    if (current) {
      if (current.owner.accountKey !== owner.accountKey || current.owner.installationId !== owner.installationId) {
        throw new Error('trace_account_switch_requires_lock')
      }
      if (sameTraceOwnerIdentity(current.owner, owner) && sameConnectionScope(current.scope, requested.scope)) {
        return { context: this.current()!, kind: 'reused' }
      }
      this.active!.rebind(owner, requested.scope)
      this.diagnostic({ code: 'trace_owner_rebound' })
      return { context: this.current()!, kind: 'rebound' }
    }

    if (this.flight) {
      await this.flight
      return this.activate({ owner, scope: requested.scope }, create)
    }

    const generation = this.generation
    const flight = create()
    this.flight = flight
    try {
      const session = await flight
      if (generation !== this.generation) {
        await session.stop(0)
        throw new Error('trace_activation_superseded')
      }
      this.active = session
      this.diagnostic({ code: 'trace_admission_ready' })
      return { context: this.current()!, kind: 'created' }
    } finally {
      if (this.flight === flight) this.flight = null
    }
  }

  trigger(reason: TraceRecoveryReason): void { this.active?.trigger(reason) }
  compactIfIdle(): Promise<boolean> { return this.active?.compactIfIdle() ?? Promise.resolve(false) }

  async lock(flushMs = 3_000): Promise<void> {
    this.generation += 1
    await this.flight?.catch(() => undefined)
    const session = this.active
    this.active = null
    if (session) await session.stop(flushMs)
    this.diagnostic({ code: 'trace_terminal_locked' })
  }
}
```

`sameConnectionScope` 可以从现有 helper 导入；如它仍留在 `main.ts`，将纯函数提取到已有 auth scope 模块并增加直接测试。

- [ ] **Step 4: 运行 runtime 全测试并提交**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-durability-runtime.test.ts
git add apps/desktop/electron/trace-durability-runtime.ts apps/desktop/electron/trace-durability-runtime.test.ts
git commit -m "feat(desktop): supervise durable trace sessions"
```

### Task 4: 将 Main 接入 runtime，消除可信状态的空 façade

**Files:**
- Modify: `apps/desktop/electron/main.ts:6132,6157,6189,8866-9280,10920-10940,14100-14120`
- Modify: `apps/desktop/electron/trace-forwarder.test.ts:1320-1470`
- Modify: `apps/desktop/electron/desktop-runtime-gate.test.ts`
- Modify: `apps/desktop/electron/trace-durability-runtime.test.ts`

- [ ] **Step 1: 先写 façade 安装和同账户 rebind 行为测试**

扩展 runtime 测试的 fake session/façade adapter，断言：

```ts
assert.deepEqual(facadeCalls, ['install'])
await coordinator.applySameAccountOwner(ownerB)
assert.deepEqual(facadeCalls, ['install'])
assert.deepEqual(backendTransportDescriptors, [stableDescriptor])
```

添加硬 lock 行为：

```ts
await coordinator.lock('signed_out')
assert.deepEqual(facadeCalls, ['install', 'detach', 'rotateBearer'])
assert.equal(runtime.current(), null)
```

这些断言通过可注入的窄 coordinator helper 执行，不读 `main.ts` 文本。

- [ ] **Step 2: 运行新测试确认 red**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-durability-runtime.test.ts
```

Expected: FAIL，Main adapter 的行为 helper 尚未存在。

- [ ] **Step 3: 收敛 Main 全局状态**

用：

```ts
const desktopTraceRuntime = new TraceDurabilityRuntime(event =>
  rememberLog(`[trace] ${JSON.stringify(event)}`)
)
let desktopTraceFacadeReady = false
```

替代 `desktopTraceContext/Forwarder/Lifecycle/OutboxStore/Generation/StartupPromise/RuntimeStartup`。

`ensureDesktopTraceFacade()` 只启动 listener，不立即 attach backend transport。
`traceContextForBackendRoot()` 只在 `desktopTraceFacadeReady && desktopTraceRuntime.current()` 时返回 descriptor。

`ensureDesktopTraceForwarder()` 改为：

```ts
async function ensureDesktopTraceForwarder(scope: ConnectionScope, requestedOwner: TraceOwner) {
  await ensureDesktopTraceFacade()
  const activation = await desktopTraceRuntime.activate(
    { owner: requestedOwner, scope },
    () => createDesktopTraceSession(scope, requestedOwner)
  )

  if (activation.kind === 'created') {
    desktopTraceFacade!.install(activation.context.ingress)
    desktopTraceFacadeReady = true
    await attachDesktopTraceTransportToRunningBackends()
    scheduleDesktopTraceTransportAttachRetry(30_000)
  }
  return activation.context
}
```

`createDesktopTraceSession()` 复用现有 store open、legacy migration、compaction、controller、forwarder 和 lifecycle 代码，但 credential 使用 Task 1 的 source。先绑定初始 loader，再构造 provider/forwarder：

```ts
const source = new RebindableTraceCredentialSource()
source.bind(requestedOwner, async () => loadCurrentDesktopTraceCredential(requestedOwner, scope))
const provider = new RefreshingTraceCredentialProvider(source, {
  installationId: desktopInstallationId
})

const rebind = (owner: TraceOwner, boundScope: ConnectionScope) => {
  source.bind(owner, async () => loadCurrentDesktopTraceCredential(owner, boundScope))
  forwarder.rebindOwner(owner)
}
```

credential loader 每次都在 bridge 调用前后验证精确 owner/scope，但任何失败都只向 uploader 抛出：

```ts
async function loadCurrentDesktopTraceCredential(
  expectedOwner: TraceOwner,
  expectedScope: ConnectionScope
): Promise<TraceCredential> {
  const bridge = desktopAuthBridge
  const status = desktopAuthCoordinator?.status('local')
  if (!bridge || !status || !sameConnectionScope(desktopAuthCoordinator?.scope('local'), expectedScope)) {
    throw new AuthBridgeError('runtime_unavailable', 'runtime_unavailable')
  }
  const before = traceOwnerFromScope(status, expectedScope, desktopInstallationId)
  if (!sameTraceOwnerIdentity(before, expectedOwner)) {
    throw new AuthBridgeError('auth_required', 'session_rejected')
  }

  const credential = await bridge.traceToken({
    installation_id: desktopInstallationId,
    client_version: app.getVersion(),
    telemetry_schema_version: '1'
  })

  const validated = desktopAuthCoordinator?.status('local')
  if (!validated || !sameConnectionScope(desktopAuthCoordinator?.scope('local'), expectedScope)) {
    throw new AuthBridgeError('runtime_unavailable', 'runtime_unavailable')
  }
  const after = traceOwnerFromScope(validated, expectedScope, desktopInstallationId)
  if (!sameTraceOwnerIdentity(after, expectedOwner)) {
    throw new AuthBridgeError('auth_required', 'session_rejected')
  }
  return credential
}
```

初次 start 前只 `source.bind()`，不调用 `forwarder.rebindOwner()`。`lifecycle.start()` 只发起不被 await 的 credential acquisition，因此 bridge/Trace 服务不能成为本地 admission 启动前置条件。`onTerminalRevocation` 直接返回 `coordinator.applyTraceTerminalRevocation(revocation)` 的 Promise<boolean>。

- [ ] **Step 4: 改成严格本地耐久化启动顺序**

在 primary backend preparation 中不再通过会吞掉启动错误的 `resolveLocalBackendWithTrace()`：

```ts
await ensureDesktopTraceForwarder(connectionScope, owner)
return resolveHermesBackend(backendArgs)
```

因为 forwarder start 不获取云端 credential，auth bridge/Trace 不可达不会导致此 await 失败。如果 outbox/Safe Storage/listener 无法建立，则在 backend 启动前以本地耐久性错误失败，不发布空 façade descriptor。

resume/online/focus 调用 `desktopTraceRuntime.trigger(...)`，空闲 compaction 调用 `desktopTraceRuntime.compactIfIdle()`。

`stopDesktopTraceForwarder()` 的顺序固定为：

```ts
desktopTraceFacadeReady = false
desktopTraceFacade?.detach()
rotateDesktopTraceIngressBearer({ reattach: false })
await desktopTraceRuntime.lock(flushMs)
```

同账户 rebind 不执行上述任何一步。

- [ ] **Step 5: 用行为测试取代被触及的源码正则测试**

删除 `trace-forwarder.test.ts` 中以下源码读取用例：

- `desktop lifecycle starts local backend through degraded Trace recovery...`
- `desktop trace startup aborts settle...`
- `every non-reused desktop trace startup rotates...`

将它们的合同分别并入 `trace-durability-runtime.test.ts` 的 activate/rebind/lock/superseded-create 测试和 `trace-continuity.integration.test.ts` 的真实 store 测试。`desktop-runtime-gate.test.ts` 中本次 Trace 生命周期的源码文本断言同样删除，其他不相关测试不改。

- [ ] **Step 6: 运行 Main 相关回归并提交**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-durability-runtime.test.ts electron/trace-forwarder.test.ts electron/desktop-runtime-gate.test.ts electron/trace-backend-registry.test.ts
npm run --workspace apps/desktop typecheck
git add apps/desktop/electron/main.ts apps/desktop/electron/trace-durability-runtime.ts apps/desktop/electron/trace-durability-runtime.test.ts apps/desktop/electron/trace-forwarder.test.ts apps/desktop/electron/desktop-runtime-gate.test.ts
git commit -m "fix(desktop): keep durable trace admission active"
```

Expected: 同账户 rebind 只记录 `trace_owner_rebound`，不出现 detach/rotate/stop。

### Task 5: 覆盖 Trace 服务/auth bridge 不可达和跨重启补传

**Files:**
- Modify: `apps/desktop/electron/trace-continuity.integration.test.ts`
- Modify: `apps/desktop/electron/trace-durability-runtime.test.ts`

- [ ] **Step 1: 先写上游和 credential 同时失败的集成测试**

使用真实 `TraceOutboxStore`、`TraceForwarder`、`TraceRecoveryController` 和临时目录：

```ts
for (let index = 0; index < 25; index += 1) {
  assert.equal((await post(endpoint, bearer, { runId: `offline-${index}` })).status, 200)
}
assert.equal((await store.diagnostics()).pending, 25)
assert.equal(upstreamCalls, 0)
```

credential loader 始终抛 `AuthBridgeError('runtime_unavailable', 'runtime_unavailable')`，上游 fetch 也不可达。测试必须证明 ingress 成功与 token loader 无关。

- [ ] **Step 2: 写关闭/重开 outbox 后补传测试**

停止第一个 runtime，使用同一 account namespace/key protector 重新 open store，切换为可用 credential/fetch，触发 pump：

```ts
await waitFor(async () => (await reopenedStore.diagnostics()).pending === 0)
assert.equal(new Set(receivedBatchIds).size, 25)
assert.equal(receipts.every(value => value === 'accepted' || value === 'duplicate'), true)
```

服务端 harness 按 idempotency key 记录 batch，重复 ID 返回 `duplicate`。

- [ ] **Step 3: 写同账户 Session 更新并发测试**

在连续 POST 中间调用 runtime rebind，断言 endpoint/bearer 全程字节相同，store 中旧/新 owner 各自存在，但 account key 相同，恢复后全部 receipt。

- [ ] **Step 4: 运行集成测试验证 Tasks 1–4 的组合行为**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-continuity.integration.test.ts
```

Expected: PASS。本任务是跨模块验证任务；生产行为已在 Tasks 1–4 分别以 red 测试驱动。如此处失败，不改 outbox 格式，而是先将失败缩小为对应模块的单元回归，再修正该模块。

- [ ] **Step 5: 运行 Trace 集成回归并提交**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-continuity.integration.test.ts electron/trace-durability-runtime.test.ts electron/trace-forwarder.test.ts electron/trace-outbox-store.test.ts
git add apps/desktop/electron/trace-continuity.integration.test.ts apps/desktop/electron/trace-durability-runtime.test.ts apps/desktop/electron/trace-durability-runtime.ts apps/desktop/electron/trace-forwarder.ts
git commit -m "test(desktop): prove trace durability through outages"
```

### Task 6: 固定存储故障、诊断和账户隔离

**Files:**
- Modify: `apps/desktop/electron/trace-durability-runtime.ts`
- Modify: `apps/desktop/electron/trace-durability-runtime.test.ts`
- Modify: `apps/desktop/electron/trace-continuity.integration.test.ts`
- Modify: `apps/desktop/electron/main.ts`

- [ ] **Step 1: 先写存储失败不假报成功测试**

注入同时失败的 local durable commit 和 upstream，断言 POST 为 503，diagnostic 为 `trace_storage_failed`，且诊断不含 secret。分别覆盖 `ENOSPC`、`secure_key_storage_unavailable` 和 journal integrity failure。

```ts
assert.equal(response.status, 503)
assert.deepEqual(events.map(event => event.code), ['trace_storage_failed'])
assert.doesNotMatch(JSON.stringify(events), /Bearer |access_token|wrappedKey|payload/)
```

- [ ] **Step 2: 先写 A/B 账户硬隔离测试**

激活 A，写入 pending batch，`lock()`，再激活 B。断言 B 的 store root/key/batch/receipt 不包含 A，A 的延迟 fetch/receipt 释放后也不改变 B diagnostics。

- [ ] **Step 3: 实现有界诊断状态转换**

runtime 使用 Task 3 已定义的 `TraceDurabilityDiagnostic` 类型，仅在状态变化时输出。事件 payload 只允许 `code`、退避次数、pending count/bytes 和安全错误类别。Main 通过 `rememberLog()` 写入本地日志，不发 Renderer telemetry。

- [ ] **Step 4: 运行安全与隔离回归并提交**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron electron/trace-durability-runtime.test.ts electron/trace-continuity.integration.test.ts electron/trace-outbox-crypto.test.ts electron/trace-outbox-store.test.ts electron/trace-outbox-journal.test.ts electron/trace-outbox-record.test.ts electron/trace-outbox-types.test.ts
git add apps/desktop/electron/trace-durability-runtime.ts apps/desktop/electron/trace-durability-runtime.test.ts apps/desktop/electron/trace-continuity.integration.test.ts apps/desktop/electron/main.ts
git commit -m "fix(desktop): isolate trace durability failures"
```

### Task 7: 全量回归、打包和真实安装验收

**Files:**
- Modify only if a test exposes a real regression: files already listed above
- Evidence: `apps/desktop/build/logs/` and the installed app's local Ansatz logs; do not commit logs

- [ ] **Step 1: 运行完整 Electron Trace 测试集**

```bash
npm exec --workspace apps/desktop -- vitest run --project electron \
  electron/trace-credential-provider.test.ts \
  electron/trace-forwarder.test.ts \
  electron/trace-ingress-facade.test.ts \
  electron/trace-runtime-startup.test.ts \
  electron/trace-recovery-controller.test.ts \
  electron/trace-continuity.integration.test.ts \
  electron/trace-outbox-crypto.test.ts \
  electron/trace-outbox-journal.test.ts \
  electron/trace-outbox-record.test.ts \
  electron/trace-outbox-store.test.ts \
  electron/trace-outbox-types.test.ts \
  electron/trace-legacy-owner.test.ts
```

Expected: PASS；无 flaky retry，无开口 handle。

- [ ] **Step 2: 运行 Desktop 全量静态与单元回归**

```bash
npm run --workspace apps/desktop typecheck
npm run --workspace apps/desktop lint
npm run --workspace apps/desktop test:desktop:platforms
```

Expected: 全部 PASS；模型、配置、会话、WebSocket、terminal 和 scope-token 测试无回归。

- [ ] **Step 3: 使用 Node 26.7.0 重建当前开发 DMG**

```bash
test "$(node --version)" = "v26.7.0"
npm run build:desktop:dmg
```

Expected: `apps/desktop/release/Ansatz-0.17.0-mac-arm64.dmg` 生成，`codesign --verify --deep --strict` 通过。该任务只验证 Trace 修复的 ad-hoc 开发包；正式 Developer ID/notarization 已延后，不是本计划阻塞。

- [ ] **Step 4: 完全卸载/重装并做真实 Trace 故障注入**

在用户明确授权卸载后，安装新 DMG，登录并配置模型。先正常发送一次模型请求，确认 outbox journal 出现 `accepted`。然后使用测试 harness 临时阻断 Trace upstream/credential loader，连续发送多个会话，确认 pending 增加且无 OTel network discard；恢复后 pending 回到 0。

验收日志不得出现：

```text
Ansatz login required
trace durability temporarily unavailable
BatchSpanProcessor.ExportError
```

除非故意注入本地磁盘/Safe Storage 故障，否则本地 ingress 不得返回 503。

- [ ] **Step 5: 检查 diff 并提交验收修正**

```bash
git diff --check
git status --short
git diff --stat c75a04b33f..HEAD
```

如验收暴露真实问题，回到导致失败的具体任务，先添加对应失败测试，然后只 stage
该任务 `Files` 列表中实际修改的文件，并使用 `fix(desktop): close trace durability regression`
提交。不得为了通过验收而放宽断言或删除故障注入。

Expected final state: worktree clean，Trace 服务/auth bridge 不可达只增加加密 pending backlog，不丢 Trace、不要求登录。
