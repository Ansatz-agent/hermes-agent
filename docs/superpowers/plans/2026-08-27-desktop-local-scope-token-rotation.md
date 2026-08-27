# Desktop Local Scope Token Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Ansatz Desktop 本地 scope token 以协议 v2 在后台完成确认式轮换，使正常轮换、账户服务临时不可达和 provider 401 都不会再触发错误的登录门，同时保留 logout、scope epoch 和 backend 生命周期的即时撤销。

**Architecture:** Electron Main 用 `BackendControlChannel` 解析 backend ready/ACK，并由每个 backend 唯一的 `LocalCapabilityManager` 执行 candidate → probe → promote → atomic descriptor switch。Python registry 区分 candidate/active/overlap，HTTP 中间件区分本地 capability 与账户锁定，WebSocket 在 upgrade 后绑定 scope/generation 而不是 bearer TTL；账户 owner 另外发布 cloud availability，使 Desktop 本地 consumer 能在已可信账户记录下继续工作。

**Tech Stack:** Electron 40、Node.js 22、TypeScript 6、Vitest、Python 3.11+、FastAPI/Starlette、aiohttp、pytest（仅通过 `scripts/run_tests.sh`）、macOS Keychain/Windows Credential Manager、Electron Builder。

---

## 执行约束

- 设计规范是 `docs/superpowers/specs/2026-08-27-desktop-local-scope-token-rotation-design.md`；实现偏离时先更新规范并获得确认。
- 保留用户已有的未跟踪文件 `docs/design/object-context-memory-improvements.md`，任何提交都不能 stage 它。
- 每个任务严格走 red → green：先运行新测试确认预期失败，再写最小实现，再运行聚焦测试。
- Python 测试只使用 `scripts/run_tests.sh`，不能直接调用 pytest。
- 测试必须执行真实函数/模块行为，不能读取 `.py`、`.ts` 或 `.tsx` 源码文本做正则断言。
- 不新增 outbound telemetry，不把 bearer、digest、Cookie、完整 registration/transition id 写入日志。
- 不修改远端账户服务 API、数据库或部署；本计划只修改 Electron、随客户端捆绑的本地 Python runtime、Renderer 的最小状态呈现和安装包配对校验。
- 不在实现中保留协议 v1 的 60 秒前台轮换兼容路径；v1 只能进入 runtime 更新/修复状态。

## 文件映射

### 新文件

- `hermes_cli/client_auth/backend_scope_protocol.py`：协议 v2 帧、ACK 编码、严格 schema 和安全常量；不拥有账户状态。
- `tests/hermes_cli/client_auth/test_backend_scope_tokens.py`：Python registry、控制流、probe、错误分类和 WS scope 行为测试。
- `apps/desktop/electron/backend-control-channel.ts`：唯一 stdout 行路由、ready capability、ACK waiter 和 stdin request/response 协调。
- `apps/desktop/electron/backend-control-channel.test.ts`：chunk 边界、乱序/重复 ACK、timeout、日志脱敏和 ready-file 行为。
- `apps/desktop/electron/local-capability-manager.ts`：每 backend 的 active/candidate、single-flight timer、probe、promote、descriptor snapshot 和 revoke。
- `apps/desktop/electron/local-capability-manager.test.ts`：fake clock 下的完整轮换与 fault injection。
- `apps/desktop/electron/backend-json-client.ts`：结构化 HTTP 错误与只针对 `pre_dispatch` capability 拒绝的一次恢复重试。
- `apps/desktop/electron/backend-json-client.test.ts`：GET/PUT retry 安全性与恰好一次业务写入。
- `apps/desktop/electron/local-backend-capability.ts`：把 spawn/ready/manager/descriptor 生命周期从 `main.ts` 提取成可测试窄接线。
- `apps/desktop/electron/local-backend-capability.test.ts`：primary/pool backend 的协议配对、清理和 scope 切换测试。
- `tests/fixtures/desktop_scope_control_backend.py`：跨进程测试 fixture，只组合真实协议/registry，不复制产品状态机。
- `apps/desktop/electron/local-capability-integration.test.ts`：Node ↔ Python stdin/stdout/HTTP 故障注入测试。

### 修改文件

- `apps/desktop/electron/auth-scope-token.ts` / `.test.ts`：30 分钟 TTL、20 分钟轮换点、60 秒 overlap、registration/transition id 和协议 v2 编码。
- `apps/desktop/electron/backend-ready.ts` / `.test.ts`：ready 结果携带 `desktopScopeProtocol`；兼容 ready file。
- `apps/desktop/electron/auth-bridge.ts` / `.test.ts`：公开 `cloud_state`，严格验证 bridge 响应。
- `apps/desktop/electron/auth-coordinator.ts` / `.test.ts`：不可达时保留 scope，只有 signed-out/locked/epoch change 执行 cleanup。
- `apps/desktop/electron/auth-runtime-contract.ts` / `.test.ts`：runtime probe 同时验证 auth bridge v1 和 desktop scope protocol v2。
- `apps/desktop/electron/main.ts`：删除 `ensureFreshDesktopScopeToken()` 前台轮换，接入新模块和结构化错误。
- `apps/desktop/src/components/auth-gate.tsx` / `.test.tsx`：`cloud_state` 不卸载 protected tree，真正 locked 才显示登录门。
- `apps/desktop/src/global.d.ts`：Renderer 只读的 cloud availability 类型；不暴露 token 生命周期。
- `hermes_cli/client_auth/runtime.py`：registry 状态机、local capability exception、WS claim、cloud availability 和 Desktop-local consumer policy。
- `hermes_cli/client_auth/bridge.py`：`cloud_state` 的严格公开协议。
- `hermes_cli/web_server.py`：candidate probe、结构化 capability 错误、WS scope/generation 验证、ready v2 广告。
- `gateway/platforms/api_server.py`：与 dashboard 一致的 pre-dispatch 错误分类。
- `hermes_cli/dashboard_auth/routes.py`：WS ticket 携带不可重放的 scope/generation claim，而不是 token digest/expiry。
- `tests/hermes_cli/client_auth/test_runtime.py`、`tests/hermes_cli/client_auth/test_boundaries.py`、`tests/hermes_cli/test_dashboard_auth_ws_auth.py`：账户连续性和真实边界回归测试。
- `apps/desktop/e2e/installed-windows-auth.spec.ts`、`apps/desktop/electron/package-runtime/windows-auth-toolchain.test.ts`：捆绑 runtime v2 验收。
- `scripts/install.sh`、`scripts/install.ps1`：安装完成 probe 验证 scope protocol v2，不新增用户配置项。

### Task 1: 固定协议 v2 的跨语言 wire contract

**Files:**
- Create: `hermes_cli/client_auth/backend_scope_protocol.py`
- Create: `tests/hermes_cli/client_auth/test_backend_scope_tokens.py`
- Modify: `apps/desktop/electron/auth-scope-token.ts`
- Modify: `apps/desktop/electron/auth-scope-token.test.ts`

- [ ] **Step 1: 先写 TypeScript 失败测试**

把 `auth-scope-token.test.ts` 的 60 秒断言替换为以下行为，并保留现有环境变量 secret 清理测试：

```ts
test('issues a 30-minute v2 candidate and rotates after 20 minutes', () => {
  const token = issueAuthScopeToken(scope, {
    clock: () => 100,
    randomBytes: size => Buffer.alloc(size, 0xa5),
    randomIdBytes: size => Buffer.alloc(size, 0x5a)
  })

  assert.equal(token.ttlSeconds, 1_800)
  assert.equal(token.rotateAt, 1_300)
  assert.equal(token.validUntil, 1_900)
  assert.equal(Buffer.from(token.bearer, 'base64url').byteLength, 32)
  assert.equal(Buffer.from(token.registrationId, 'base64url').byteLength, 16)
})

test('encodes strict v2 register and promote frames without logging secrets', () => {
  const token = issueAuthScopeToken(scope, {
    clock: () => 100,
    randomBytes: size => Buffer.alloc(size, 0xa5),
    randomIdBytes: size => Buffer.alloc(size, 0x5a)
  })
  const transitionId = issueScopeTransitionId(() => Buffer.alloc(16, 0x33))

  assert.deepEqual(JSON.parse(encodeScopeTokenRegistration(token)), {
    version: 2,
    operation: 'register_scope_token',
    registration_id: token.registrationId,
    bearer: token.bearer,
    connection_id: 'local',
    runtime_instance_id: scope.runtime_instance_id,
    epoch: 7,
    ttl_seconds: 1_800
  })
  assert.deepEqual(
    JSON.parse(encodeScopeTokenPromotion(token, null, transitionId)),
    {
      version: 2,
      operation: 'promote_scope_token',
      transition_id: transitionId,
      registration_id: token.registrationId,
      previous_registration_id: null,
      connection_id: 'local',
      runtime_instance_id: scope.runtime_instance_id,
      epoch: 7,
      overlap_seconds: 60
    }
  )
})
```

- [ ] **Step 2: 运行 TypeScript 测试，确认 red**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-scope-token.test.ts
```

Expected: FAIL；旧常量仍为 60，且 `rotateAt`、`registrationId`、promotion encoder 不存在。

- [ ] **Step 3: 实现 TypeScript 常量、类型和编码器**

在 `auth-scope-token.ts` 中保留现有 child environment sanitization，并用以下公开 contract 替换 v1 token 部分：

```ts
export const DESKTOP_SCOPE_PROTOCOL_VERSION = 2
export const AUTH_SCOPE_TOKEN_TTL_SECONDS = 1_800
export const AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS = 1_200
export const AUTH_SCOPE_TOKEN_OVERLAP_SECONDS = 60

export type AuthScopeToken = {
  bearer: string
  registrationId: string
  scope: ConnectionScope
  issuedAt: number
  rotateAt: number
  ttlSeconds: number
  validUntil: number
}

export type ScopeTokenRegisteredAck = {
  version: 2
  operation: 'scope_token_registered'
  registration_id: string
  connection_id: string
  runtime_instance_id: string
  epoch: number
  ttl_seconds: number
}

export type ScopeTokenPromotedAck = {
  version: 2
  operation: 'scope_token_promoted'
  transition_id: string
  registration_id: string
  previous_registration_id: string | null
  connection_id: string
  runtime_instance_id: string
  epoch: number
  overlap_seconds: number
}

export type ScopeControlAck = ScopeTokenRegisteredAck | ScopeTokenPromotedAck

type IssueAuthScopeTokenOptions = {
  clock?: () => number
  randomBytes?: (size: number) => Buffer
  randomIdBytes?: (size: number) => Buffer
}

export function issueScopeTransitionId(randomBytes = nodeRandomBytes): string {
  return randomBytes(16).toString('base64url')
}

export function issueAuthScopeToken(
  scope: ConnectionScope,
  options: IssueAuthScopeTokenOptions = {}
): AuthScopeToken {
  const required = requireAuthenticatedConnectionScope(scope)
  const clock = options.clock ?? uptime
  const bearerSource = options.randomBytes ?? nodeRandomBytes
  const idSource = options.randomIdBytes ?? nodeRandomBytes
  const issuedAt = clock()
  const bearer = bearerSource(32).toString('base64url')
  const registrationId = idSource(16).toString('base64url')

  if (Buffer.from(bearer, 'base64url').byteLength !== 32) {
    throw new Error('Auth scope token entropy source returned an invalid value')
  }
  if (Buffer.from(registrationId, 'base64url').byteLength !== 16) {
    throw new Error('Auth scope registration id source returned an invalid value')
  }

  return {
    bearer,
    registrationId,
    scope: { ...required },
    issuedAt,
    rotateAt: issuedAt + AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS,
    ttlSeconds: AUTH_SCOPE_TOKEN_TTL_SECONDS,
    validUntil: issuedAt + AUTH_SCOPE_TOKEN_TTL_SECONDS
  }
}

export function encodeScopeTokenRegistration(token: AuthScopeToken): string {
  return boundedFrame({
    version: DESKTOP_SCOPE_PROTOCOL_VERSION,
    operation: 'register_scope_token',
    registration_id: token.registrationId,
    bearer: token.bearer,
    connection_id: token.scope.connection_id,
    runtime_instance_id: token.scope.runtime_instance_id,
    epoch: token.scope.epoch,
    ttl_seconds: token.ttlSeconds
  })
}

export function encodeScopeTokenPromotion(
  token: AuthScopeToken,
  previousRegistrationId: string | null,
  transitionId: string
): string {
  return boundedFrame({
    version: DESKTOP_SCOPE_PROTOCOL_VERSION,
    operation: 'promote_scope_token',
    transition_id: transitionId,
    registration_id: token.registrationId,
    previous_registration_id: previousRegistrationId,
    connection_id: token.scope.connection_id,
    runtime_instance_id: token.scope.runtime_instance_id,
    epoch: token.scope.epoch,
    overlap_seconds: AUTH_SCOPE_TOKEN_OVERLAP_SECONDS
  })
}

function boundedFrame(value: Record<string, unknown>): string {
  const frame = `${JSON.stringify(value)}\n`
  if (Buffer.byteLength(frame) > 4_096) {
    throw new Error('Auth scope control frame is too large')
  }
  return frame
}
```

- [ ] **Step 4: 写 Python wire contract 失败测试并实现纯协议模块**

在新测试文件中先断言 strict keys、1800 秒上限、16-byte id 和 ACK 不含 bearer；运行后确认 import failure，再创建 `backend_scope_protocol.py`：

```python
DESKTOP_SCOPE_PROTOCOL_VERSION = 2
BACKEND_SCOPE_TOKEN_TTL_SECONDS = 1_800
BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS = 60
BACKEND_SCOPE_CONTROL_FRAME_LIMIT = 4_096
CONTROL_ACK_PREFIX = "ANSATZ_SCOPE_CONTROL_V2 "


@dataclass(frozen=True)
class ScopeTokenRegistration:
    registration_id: str
    bearer: str
    connection_id: str
    runtime_instance_id: str
    epoch: int
    ttl_seconds: float


@dataclass(frozen=True)
class ScopeTokenPromotion:
    transition_id: str
    registration_id: str
    previous_registration_id: str | None
    connection_id: str
    runtime_instance_id: str
    epoch: int
    overlap_seconds: float


def encode_control_ack(payload: dict[str, object]) -> bytes:
    if "bearer" in payload or "token_digest" in payload:
        raise ValueError("control ack contains a secret")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    encoded = f"{CONTROL_ACK_PREFIX}{body}\n".encode("ascii")
    if len(encoded) > BACKEND_SCOPE_CONTROL_FRAME_LIMIT:
        raise ValueError("control ack is too large")
    return encoded
```

解析函数必须对 register/promote 使用精确 key set，校验 base64url 长度、scope、TTL/overlap 固定上限，并返回以上 dataclass；未知 operation 抛 `ValueError`。

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_backend_scope_tokens.py -q
```

Expected after implementation: PASS。

- [ ] **Step 5: 运行跨语言聚焦测试并提交**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-scope-token.test.ts
cd ../..
scripts/run_tests.sh tests/hermes_cli/client_auth/test_backend_scope_tokens.py -q
git add apps/desktop/electron/auth-scope-token.ts apps/desktop/electron/auth-scope-token.test.ts hermes_cli/client_auth/backend_scope_protocol.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py
git commit -m "feat(auth): define desktop scope protocol v2"
```

Expected: 两组测试全部通过；commit 只含四个列出的文件。

### Task 2: 实现 Python candidate/active/overlap registry 与 ACK 控制流

**Files:**
- Modify: `hermes_cli/client_auth/runtime.py`
- Modify: `tests/hermes_cli/client_auth/test_backend_scope_tokens.py`
- Modify: `tests/hermes_cli/client_auth/test_runtime.py`

- [ ] **Step 1: 写 registry 状态机失败测试**

增加以下四类测试：

```python
def test_candidate_cannot_authorize_business_until_promoted():
    registry, scope, clock = registry_fixture()
    candidate = registry.register_candidate(registration("A"), expected=scope)
    assert registry.probe(_scope_bearer(b"A")).registration_id == candidate.registration_id
    with pytest.raises(BackendScopeTokenRejected, match="candidate_not_active"):
        registry.authorize(_scope_bearer(b"A"), "dashboard.api.request")


def test_promote_atomically_activates_candidate_and_bounds_old_overlap():
    registry, scope, clock = registry_fixture()
    first = register_and_promote(registry, scope, b"A", previous=None)
    second = registry.register_candidate(registration("B"), expected=scope)
    registry.promote(promotion(second, previous=first), expected=scope)
    assert registry.authorize(_scope_bearer(b"B"), "dashboard.api.request").state == "active"
    assert registry.authorize(_scope_bearer(b"A"), "dashboard.api.request").state == "overlap"
    clock.now += 60
    with pytest.raises(BackendScopeTokenRejected, match="expired"):
        registry.authorize(_scope_bearer(b"A"), "dashboard.api.request")


def test_duplicate_transition_is_idempotent_but_conflicting_reuse_is_rejected():
    registry, scope, _clock = registry_fixture()
    first = register_and_promote(registry, scope, b"A", previous=None)
    duplicate = registry.promote(promotion(first, previous=None), expected=scope)
    assert duplicate.registration_id == first.registration_id
    with pytest.raises(BackendScopeTokenRejected, match="transition_conflict"):
        registry.promote(conflicting_promotion(first), expected=scope)


def test_clear_rotates_backend_generation_and_invalidates_ws_claims():
    registry, scope, _clock = registry_fixture()
    active = register_and_promote(registry, scope, b"A", previous=None)
    claim = registry.ws_claim(active)
    registry.clear()
    with pytest.raises(BackendScopeTokenRejected, match="backend_generation_changed"):
        registry.authorize_ws_claim(claim, "dashboard.ws.message")
```

- [ ] **Step 2: 运行测试，确认 red**

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_backend_scope_tokens.py -q
```

Expected: FAIL；v1 registry 没有 candidate、promotion、probe、backend generation 和专用 exception。

- [ ] **Step 3: 在 runtime 中实现最小状态机**

新增并在旧 import surface 上继续 re-export：

```python
class BackendScopeTokenRejected(RuntimeError):
    code = "local_capability_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failure_phase = "pre_dispatch"


class BackendScopeGrantState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    OVERLAP = "overlap"


@dataclass(frozen=True)
class BackendScopeGrant:
    registration_id: str
    connection_id: str
    auth: AuthScope
    state: BackendScopeGrantState
    valid_until: float
    token_digest: str
    promoted_transition_id: str | None = None


@dataclass(frozen=True)
class BackendScopeWsClaim:
    connection_id: str
    runtime_instance_id: str
    epoch: int
    backend_generation: str
```

`BackendScopeTokenRegistry` 同时维护 digest map、registration map、transition map 和随机
`backend_generation`。实现下列公开方法，并让所有 mutation 位于同一个 `RLock`：

```python
register_candidate(registration, *, expected) -> BackendScopeGrant
promote(promotion, *, expected) -> BackendScopeGrant
probe(bearer) -> BackendScopeGrant
authorize(bearer, boundary, *, connection_id=None) -> BackendScopeGrant
ws_claim(grant) -> dict[str, object]
authorize_ws_claim(claim, boundary) -> AuthScope
revoke(*, connection_id, expected) -> None
clear() -> None
```

`register_candidate()` 先执行账户 `authorize`，只存 SHA-256 digest；`authorize()` 只接受
active/未过期 overlap；`promote()` 验证 previous 精确等于当前 active 后一次性提升 candidate、
截短旧 grant；`clear()` 清空 maps 并生成新 backend generation。

- [ ] **Step 4: 改造 stdin control reader 输出 ACK，并验证 EOF**

把 `_run_backend_scope_token_control` 改为接收 `source` 与 `target`：先 parse v2 frame，再调用
registry，最后 `target.write(encode_control_ack(...)); target.flush()`。register ACK 只包含
`version/operation/registration_id/connection_id/runtime_instance_id/epoch/ttl_seconds`；promote ACK
只包含 transition、registration、previous、scope 和 overlap。任何 malformed frame 终止 reader；
`finally` 中 `registry.clear()`。

更新 EOF 测试，使它同时断言：ACK 在 registry mutation 后出现、ACK 不含 bearer、EOF 后 active
HTTP grant 与 WS claim 都失败。

- [ ] **Step 5: 运行回归并提交**

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_backend_scope_tokens.py tests/hermes_cli/client_auth/test_runtime.py -q
git add hermes_cli/client_auth/runtime.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py tests/hermes_cli/client_auth/test_runtime.py
git commit -m "feat(auth): add two-phase backend scope registry"
```

Expected: 新状态机和既有 owner/runtime tests 全部通过。

### Task 3: 增加无副作用 probe 与本地 capability 错误分类

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `gateway/platforms/api_server.py`
- Modify: `tests/hermes_cli/client_auth/test_boundaries.py`
- Modify: `tests/hermes_cli/client_auth/test_backend_scope_tokens.py`

- [ ] **Step 1: 写 middleware 失败测试**

覆盖精确响应：

```python
assert missing_scope.status_code == 401
assert missing_scope.json() == {
    "detail": "Local capability rejected",
    "code": "local_capability_rejected",
    "reason": "unknown",
    "failure_phase": "pre_dispatch",
    "retryable": True,
}

assert account_locked.status_code == 401
assert account_locked.json()["code"] == "account_locked"
assert account_locked.json()["detail"] == "Ansatz login required"

assert candidate_probe.status_code == 200
assert candidate_probe.json() == {
    "protocol_version": 2,
    "registration_id": registration_id,
    "connection_id": "local",
    "runtime_instance_id": scope.runtime_instance_id,
    "epoch": scope.epoch,
    "state": "candidate",
    "promoted_transition_id": None,
}
assert business_handler.call_count == 0
```

为 aiohttp gateway 写同样的 local/account 分类；provider handler 返回的 401-shaped payload 不得被
middleware 改写。

- [ ] **Step 2: 运行边界测试，确认 red**

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_boundaries.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py -q
```

Expected: FAIL；当前 middleware 把所有异常统一返回 `login_required`，且没有 candidate probe。

- [ ] **Step 3: 实现 dashboard probe 和两类异常映射**

在 `web_server.py` 中定义固定 path `/api/auth/scope-token-probe`。Desktop scope middleware 对该
path 直接调用 `backend_scope_tokens.probe(bearer)` 并返回上述 JSON，不调用 `call_next`；其他
`/api/` path 调用 active-only `authorize()`。

异常顺序必须固定：

```python
except BackendScopeTokenRejected as error:
    return JSONResponse(
        status_code=401,
        content={
            "detail": "Local capability rejected",
            "code": error.code,
            "reason": error.reason,
            "failure_phase": error.failure_phase,
            "retryable": True,
        },
    )
except AuthRequired:
    return JSONResponse(
        status_code=401,
        content={
            "detail": "Ansatz login required",
            "code": "account_locked",
            "hint": "Run `ansatz login` and retry.",
        },
    )
```

候选 probe response 不返回 bearer、digest、valid_until 或 username。

- [ ] **Step 4: 在 aiohttp gateway 使用相同机器语义**

提取一个小的 pure payload helper 供 Starlette/aiohttp adapter 调用，保持字段完全一致；只在
Desktop `HERMES_DESKTOP=1` 路径捕获 `BackendScopeTokenRejected`。普通 gateway 的账户门禁行为
保持不变。

- [ ] **Step 5: 运行测试并提交**

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_boundaries.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py tests/gateway/test_api_server.py -q
git add hermes_cli/web_server.py gateway/platforms/api_server.py tests/hermes_cli/client_auth/test_boundaries.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py
git commit -m "fix(auth): separate local capability from account lock"
```

Expected: 本地 token 错误不再含 `login_required`；真实账户 locked 仍返回登录语义。

### Task 4: 用单一 BackendControlChannel 解析 stdout ready 与 ACK

**Files:**
- Create: `apps/desktop/electron/backend-control-channel.ts`
- Create: `apps/desktop/electron/backend-control-channel.test.ts`
- Modify: `apps/desktop/electron/backend-ready.ts`
- Modify: `apps/desktop/electron/backend-ready.test.ts`
- Modify: `hermes_cli/web_server.py`

- [ ] **Step 1: 写 chunk/ACK/ready 失败测试**

```ts
test('routes split ready and ACK lines without leaking control payload to logs', async () => {
  const child = fakeChild()
  const logs: string[] = []
  const channel = new BackendControlChannel(child, { onLog: line => logs.push(line) })
  const ready = channel.waitForReady({ timeoutMs: 1_000 })
  const ack = channel.expectAck(value => value.operation === 'scope_token_registered', 1_000)

  child.stdout.write('normal log\nHERMES_BACKEND_READY port=')
  child.stdout.write('54321 desktop_scope_protocol=2\nANSATZ_SCOPE_CONTROL_V2 {"version":2,')
  child.stdout.write('"operation":"scope_token_registered","registration_id":"abc"}\n')

  assert.deepEqual(await ready, { port: 54_321, desktopScopeProtocol: 2 })
  assert.equal((await ack).registration_id, 'abc')
  assert.deepEqual(logs, ['normal log'])
})

test('ignores duplicate or unmatched ACKs and times out a specific waiter', async () => {
  vi.useFakeTimers()
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  const wait = channel.expectAck(value => value.transition_id === 'wanted', 500)
  child.stdout.write('ANSATZ_SCOPE_CONTROL_V2 {"version":2,"operation":"scope_token_promoted","transition_id":"old"}\n')
  await vi.advanceTimersByTimeAsync(501)
  await assert.rejects(wait, /scope control ACK timeout/)
})
```

- [ ] **Step 2: 运行测试，确认 red**

```bash
cd apps/desktop
npx vitest run --project electron electron/backend-control-channel.test.ts electron/backend-ready.test.ts
```

Expected: FAIL；新 channel 不存在，ready 只返回 number。

- [ ] **Step 3: 实现 stdout multiplexer 和无竞态 request**

`BackendControlChannel` 构造时立即绑定 stdout，逐行处理。公开 API 固定为：

```ts
export type BackendReady = { port: number; desktopScopeProtocol: number | null }

export type ChildProcessLike = {
  stdin: NodeJS.WritableStream & { destroyed?: boolean; writable?: boolean }
  stdout: NodeJS.ReadableStream
  on(event: 'error' | 'exit', listener: (...args: any[]) => void): unknown
  off(event: 'error' | 'exit', listener: (...args: any[]) => void): unknown
}

export class BackendControlChannel {
  constructor(child: ChildProcessLike, options: { onLog: (line: string) => void })
  waitForReady(options?: { readyFile?: string; timeoutMs?: number }): Promise<BackendReady>
  expectAck(match: (value: ScopeControlAck) => boolean, timeoutMs: number): Promise<ScopeControlAck>
  request(frame: string, match: (value: ScopeControlAck) => boolean, timeoutMs?: number): Promise<ScopeControlAck>
  close(reason?: Error): void
}
```

`request()` 必须先注册 waiter，再调用 `stdin.write(frame)`；write callback 只表示传输完成，最终
Promise 只能由 matching ACK resolve。child exit/error/stdin failure 统一 reject 所有 waiter。
parser 只接受 `ANSATZ_SCOPE_CONTROL_V2 ` 前缀后严格 JSON object；控制行永不传给 `onLog`。

- [ ] **Step 4: 广告并解析 ready protocol v2**

Python ready stdout 改为：

```python
print(
    f"{ready_token} port={actual_port} desktop_scope_protocol=2",
    flush=True,
)
```

ready file JSON 增加 `desktop_scope_protocol: 2`。`backend-ready.ts` 的 pure parser 返回
`BackendReady`，旧行没有字段时返回 `desktopScopeProtocol: null`，供 runtime repair 分支识别，
不能默认假装支持 v2。

- [ ] **Step 5: 运行测试并提交**

```bash
cd apps/desktop
npx vitest run --project electron electron/backend-control-channel.test.ts electron/backend-ready.test.ts
cd ../..
scripts/run_tests.sh tests/hermes_cli/client_auth/test_backend_scope_tokens.py -q
git add apps/desktop/electron/backend-control-channel.ts apps/desktop/electron/backend-control-channel.test.ts apps/desktop/electron/backend-ready.ts apps/desktop/electron/backend-ready.test.ts hermes_cli/web_server.py
git commit -m "feat(desktop): add acknowledged backend control channel"
```

Expected: ready/ACK chunk、timeout、exit 和日志测试全部通过。

### Task 5: 实现 LocalCapabilityManager 的后台 single-flight 轮换

**Files:**
- Create: `apps/desktop/electron/local-capability-manager.ts`
- Create: `apps/desktop/electron/local-capability-manager.test.ts`
- Modify: `apps/desktop/electron/auth-scope-token.ts`

- [ ] **Step 1: 写完整 happy-path 失败测试**

使用 fake clock/control/probe，测试初始激活与定时轮换：

```ts
test('keeps the old descriptor until candidate ACK, probe, and promotion all finish', async () => {
  const fixture = managerFixture()
  await fixture.manager.activate(fixture.binding)
  const first = fixture.manager.snapshot('backend-1')

  fixture.clock.now = first.rotateAt
  const rotating = fixture.manager.refresh('backend-1', 'timer')
  await fixture.control.waitForOperation('register_scope_token')
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)

  fixture.control.ackRegistered()
  await fixture.probe.waitForCall()
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)

  fixture.probe.resolveCandidate()
  await fixture.control.waitForOperation('promote_scope_token')
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)

  fixture.control.ackPromoted()
  await rotating
  assert.notEqual(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)
})

test('coalesces one hundred concurrent timer and recovery signals', async () => {
  const fixture = managerFixture()
  await fixture.manager.activate(fixture.binding)
  const refreshes = Array.from({ length: 100 }, (_, index) =>
    fixture.manager.refresh('backend-1', index % 2 === 0 ? 'timer' : 'recovery')
  )
  fixture.completePendingRotation()
  await Promise.all(refreshes)
  assert.equal(fixture.control.operationCount('register_scope_token'), 2)
  assert.equal(fixture.control.operationCount('promote_scope_token'), 2)
})
```

计数为 2 是“初始激活一次 + 本轮 single-flight 一次”。

- [ ] **Step 2: 写 fault-path 失败测试并运行 red**

增加以下断言：

- register ACK timeout：旧 active 不变，candidate 清理，按有界退避重试。
- probe 失败：不发送 promote，旧 active 不变。
- promote ACK 丢失但 probe 返回相同 `promoted_transition_id`：确认切换。
- 旧/重复/其他 scope ACK：被忽略直至正确 ACK 或 timeout。
- logout/profile generation change：取消 timer/promise，晚到 ACK 不能复活。
- 连续失败到 active 到期：抛 `LocalBackendCapabilityUnavailableError`，不抛 auth/login error。

Run:

```bash
cd apps/desktop
npx vitest run --project electron electron/local-capability-manager.test.ts
```

Expected: collection FAIL；manager 文件不存在。

- [ ] **Step 3: 实现 manager 的公开类型与不可变 snapshot**

```ts
export type LocalCapabilityBinding = {
  key: string
  baseUrl: string
  scope: ConnectionScope
  backendGeneration: number
  control: BackendControlChannel
}

export type LocalCapabilitySnapshot = {
  key: string
  bearer: string
  registrationId: string
  scope: ConnectionScope
  backendGeneration: number
  issuedAt: number
  rotateAt: number
  validUntil: number
}

export type RotationReason = 'timer' | 'recovery'

export type LocalCapabilityManagerOptions = {
  clock?: () => number
  issueToken?: (scope: ConnectionScope) => AuthScopeToken
  issueTransitionId?: () => string
  probe?: (baseUrl: string, bearer: string) => Promise<{
    protocol_version: 2
    registration_id: string
    connection_id: string
    runtime_instance_id: string
    epoch: number
    state: 'candidate' | 'active' | 'overlap'
    promoted_transition_id: string | null
  }>
  onDiagnostic?: (event: {
    name: string
    backendGeneration: number
    attempt: number
    elapsedMs: number
  }) => void
}

export class LocalBackendCapabilityUnavailableError extends Error {
  readonly code = 'local_backend_unavailable'
}

export class LocalCapabilityManager {
  constructor(options: LocalCapabilityManagerOptions = {})
  activate(binding: LocalCapabilityBinding): Promise<LocalCapabilitySnapshot>
  snapshot(key: string): LocalCapabilitySnapshot
  refresh(key: string, reason: RotationReason): Promise<LocalCapabilitySnapshot>
  revoke(key: string): void
  revokeByControl(control: BackendControlChannel): void
}
```

内部 state 固定为 `{binding, active, candidate, refreshPromise, timer, retryAttempt}`。`snapshot()`
返回 copy，不能让调用方 mutation 写回 manager。

- [ ] **Step 4: 实现 candidate → probe → promote 和 timer**

`activate()` 使用 previous `null` 跑同一条状态机。`refresh()` 若已有 `refreshPromise` 直接返回；
否则：

1. issue candidate；
2. `control.request(registerFrame, exactRegisteredAck)`；
3. GET `/api/auth/scope-token-probe` 并精确比较 registration/scope；
4. issue transition；
5. `control.request(promoteFrame, exactPromotedAck)`；
6. ACK timeout 时用 candidate 再 probe 一次，只接受相同 transition；
7. 原子替换 active，清 candidate，按新 token `rotateAt` 安排 unref timer。

retry delay 固定为 `[1s, 2s, 5s, 10s, 30s]` 加 0–20% jitter，且不能排到旧 active
`validUntil` 之后。诊断 callback 只收 event name、耗时、attempt 和 backend generation。

- [ ] **Step 5: 运行测试并提交**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-scope-token.test.ts electron/backend-control-channel.test.ts electron/local-capability-manager.test.ts
git add electron/auth-scope-token.ts electron/local-capability-manager.ts electron/local-capability-manager.test.ts
git commit -m "feat(desktop): rotate local capability tokens in background"
```

Expected: manager 的 happy/fault/single-flight/fake-clock tests 全部通过。

### Task 6: 接入 primary/pool backend 并删除前台轮换路径

**Files:**
- Create: `apps/desktop/electron/local-backend-capability.ts`
- Create: `apps/desktop/electron/local-backend-capability.test.ts`
- Modify: `apps/desktop/electron/main.ts`
- Modify: `apps/desktop/electron/backend-connection-state.test.ts`

- [ ] **Step 1: 写可测试接线的失败测试**

```ts
test('attaches stdout before ready, rejects v1 as runtime mismatch, and never returns an unconfirmed token', async () => {
  const fixture = lifecycleFixture({ protocol: 1 })
  await assert.rejects(
    fixture.prepare(),
    error => error instanceof LocalRuntimeProtocolError && error.code === 'local_runtime_protocol_mismatch'
  )
  assert.equal(fixture.rendererDescriptors.length, 0)
})

test('primary and pool descriptors only snapshot the manager active token', async () => {
  const fixture = lifecycleFixture({ protocol: 2 })
  const first = await fixture.prepare('primary')
  const pool = await fixture.prepare('pool:research')
  assert.equal(first.token, fixture.manager.snapshot('primary').bearer)
  assert.equal(pool.token, fixture.manager.snapshot('pool:research').bearer)
  assert.equal(fixture.foregroundRefreshCalls, 0)
})

test('child exit revokes timers, waiters, descriptors, and stdin grants', async () => {
  const fixture = lifecycleFixture({ protocol: 2 })
  await fixture.prepare('primary')
  fixture.exitChild('primary')
  assert.throws(() => fixture.manager.snapshot('primary'), /local_backend_unavailable/)
  assert.equal(fixture.child('primary').stdinEnded, true)
})
```

- [ ] **Step 2: 运行测试，确认 red**

```bash
cd apps/desktop
npx vitest run --project electron electron/local-backend-capability.test.ts electron/backend-connection-state.test.ts
```

Expected: FAIL；窄接线模块和 protocol mismatch error 不存在。

- [ ] **Step 3: 创建 lifecycle 模块**

模块公开：

```ts
export class LocalRuntimeProtocolError extends Error {
  readonly code = 'local_runtime_protocol_mismatch'
}

export async function prepareLocalBackendCapability(options: {
  key: string
  child: ChildProcessLike
  scope: ConnectionScope
  readyFile?: string
  manager: LocalCapabilityManager
  onLog: (line: string) => void
}): Promise<{ baseUrl: string; control: BackendControlChannel; snapshot: LocalCapabilitySnapshot }> {
  const control = new BackendControlChannel(options.child, { onLog: options.onLog })
  const ready = await control.waitForReady({ readyFile: options.readyFile })
  if (ready.desktopScopeProtocol !== DESKTOP_SCOPE_PROTOCOL_VERSION) {
    control.close(new LocalRuntimeProtocolError())
    throw new LocalRuntimeProtocolError()
  }
  const baseUrl = `http://127.0.0.1:${ready.port}`
  const snapshot = await options.manager.activate({
    key: options.key,
    baseUrl,
    scope: options.scope,
    backendGeneration: nextBackendGeneration(),
    control
  })
  return { baseUrl, control, snapshot }
}
```

`nextBackendGeneration()` 是 Electron 进程内递增整数，只用于拒绝晚到事件，不跨 Renderer。
实现为模块私有 counter：

```ts
let backendGeneration = 0

function nextBackendGeneration(): number {
  backendGeneration += 1
  return backendGeneration
}
```

- [ ] **Step 4: 修改 main.ts 的两条 spawn 路径**

删除 `desktopBackendScopeTokens`、`writeBackendScopeToken()`、
`ensureFreshDesktopScopeToken()` 和 `forgetDesktopBackendScopeTokens()`。primary 与 pool 都按以下顺序：

1. spawn child；
2. 立即创建 control channel；
3. 注册 child ownership/exit handler；
4. wait ready + protocol v2；
5. manager initial activate；
6. 用 active token 跑正常 `/api/status` 和 WS probe；
7. 返回 descriptor。

所有 `ensureBackend()`、registry backend 和 IPC handler 只调用同步 `manager.snapshot(key)` 装饰
descriptor，不执行 token I/O。child exit/stop 调用 `manager.revoke(key)` 后 `stdin.end()`。
protocol mismatch 送入现有 runtime update/repair 梯子，错误文案保持 local runtime，不调用 auth
coordinator cleanup。

- [ ] **Step 5: 运行 Desktop 回归并提交**

```bash
cd apps/desktop
npx vitest run --project electron electron/local-backend-capability.test.ts electron/local-capability-manager.test.ts electron/backend-connection-state.test.ts electron/backend-ownership.test.ts electron/primary-backend-startup.test.ts
npm run typecheck
git add electron/local-backend-capability.ts electron/local-backend-capability.test.ts electron/main.ts electron/backend-connection-state.test.ts
git commit -m "refactor(desktop): wire confirmed local capabilities"
```

Expected: 没有任何前台 call site 能签发或注册 token；primary/pool 行为一致。

### Task 7: 结构化 HTTP 错误与一次安全恢复重试

**Files:**
- Create: `apps/desktop/electron/backend-json-client.ts`
- Create: `apps/desktop/electron/backend-json-client.test.ts`
- Modify: `apps/desktop/electron/main.ts`

- [ ] **Step 1: 写错误解析和 exactly-once 失败测试**

```ts
test('retries one local pre-dispatch rejection with a newer confirmed descriptor', async () => {
  let handlerWrites = 0
  const manager = fakeManager(['old-token', 'new-token'])
  const transport = vi
    .fn()
    .mockRejectedValueOnce(
      new BackendHttpError(401, {
        code: 'local_capability_rejected',
        failure_phase: 'pre_dispatch',
        retryable: true
      })
    )
    .mockImplementationOnce(async () => {
      handlerWrites += 1
      return { ok: true }
    })

  const result = await requestJsonWithLocalCapability({ manager, key: 'primary', transport })
  assert.deepEqual(result, { ok: true })
  assert.equal(handlerWrites, 1)
  assert.equal(transport.mock.calls.length, 2)
})

test('never retries account, provider, malformed, or post-dispatch errors', async () => {
  for (const error of [
    new BackendHttpError(401, { code: 'account_locked' }),
    new BackendHttpError(401, { code: 'provider_unauthorized' }),
    new BackendHttpError(500, { code: 'local_capability_rejected', failure_phase: 'post_dispatch' }),
    new Error('network reset')
  ]) {
    const transport = vi.fn().mockRejectedValue(error)
    await assert.rejects(requestJsonWithLocalCapability({ manager: fakeManager(['one']), key: 'primary', transport }))
    assert.equal(transport.mock.calls.length, 1)
  }
})
```

- [ ] **Step 2: 运行测试，确认 red**

```bash
cd apps/desktop
npx vitest run --project electron electron/backend-json-client.test.ts
```

Expected: FAIL；当前 `fetchJson()` 只抛拼接字符串 Error。

- [ ] **Step 3: 提取低层 HTTP client 与 typed error**

`BackendHttpError` 保存 `status/code/reason/failurePhase/retryable/bodyPreview`；bodyPreview 最多 200
字符并经过现有 secret sanitizer。`fetchJson()` 在非 2xx 时尝试解析 JSON，但不把原始 response
或 token 放入 error message。

```ts
export class BackendHttpError extends Error {
  readonly status: number
  readonly code: string | null
  readonly reason: string | null
  readonly failurePhase: string | null
  readonly retryable: boolean
  readonly bodyPreview: string

  constructor(status: number, body: Record<string, unknown>, bodyPreview = '') {
    super(`Backend request failed (${status})`)
    this.name = 'BackendHttpError'
    this.status = status
    this.code = typeof body.code === 'string' ? body.code : null
    this.reason = typeof body.reason === 'string' ? body.reason : null
    this.failurePhase = typeof body.failure_phase === 'string' ? body.failure_phase : null
    this.retryable = body.retryable === true
    this.bodyPreview = bodyPreview.slice(0, 200)
  }
}
```

```ts
export function isRetryableLocalCapabilityError(error: unknown): error is BackendHttpError {
  return (
    error instanceof BackendHttpError &&
    error.status === 401 &&
    error.code === 'local_capability_rejected' &&
    error.failurePhase === 'pre_dispatch' &&
    error.retryable === true
  )
}
```

- [ ] **Step 4: 实现只换 descriptor 的单次 retry**

`requestJsonWithLocalCapability()` 捕获上述精确类型后调用
`manager.refresh(key, 'recovery')`，确认 returned registration id 与首个 snapshot 不同，再用原始
method/path/body bytes 重试一次。第二次任何错误直接上抛。它不能更改 request body、重复创建
upload stream 或对 OAuth/remote descriptor 生效；streaming upload/download 保持无自动 retry。

把 `main.ts` 的普通 local JSON GET/POST/PUT helper 接到该 wrapper；provider 401 保留原错误上下文，
不能调用 `desktopAuthCoordinator.logout/cleanup`。

- [ ] **Step 5: 运行测试并提交**

```bash
cd apps/desktop
npx vitest run --project electron electron/backend-json-client.test.ts electron/local-capability-manager.test.ts
npm run typecheck
git add electron/backend-json-client.ts electron/backend-json-client.test.ts electron/main.ts
git commit -m "fix(desktop): retry only rejected pre-dispatch capabilities"
```

Expected: 模型配置类写入在注入的首次 pre-dispatch 失败下业务执行恰好一次。

### Task 8: 让已建立 WebSocket 绑定 scope/generation 而非 bearer TTL

**Files:**
- Modify: `hermes_cli/client_auth/runtime.py`
- Modify: `hermes_cli/dashboard_auth/routes.py`
- Modify: `hermes_cli/web_server.py`
- Modify: `tests/hermes_cli/test_dashboard_auth_ws_auth.py`
- Modify: `tests/hermes_cli/client_auth/test_backend_scope_tokens.py`
- Modify: `apps/desktop/electron/main.ts`

- [ ] **Step 1: 先把旧“token 到期关闭 socket”测试改成新 contract**

```python
@pytest.mark.asyncio
async def test_established_scope_socket_survives_bearer_rotation_and_expiry():
    ws, registry, first, scope, clock, _current_scope = established_scope_socket()
    second = register_and_promote(registry, scope, b"B", previous=first)
    clock.now = max(first.valid_until, second.valid_until) + 1
    assert await web_server._ws_client_runtime_authorized(ws, "dashboard.ws.message")
    assert ws.closed is None


@pytest.mark.asyncio
async def test_established_scope_socket_closes_on_epoch_or_backend_generation_change():
    ws, registry, _first, scope, _clock, current_scope = established_scope_socket()
    current_scope[0] = AuthScope(scope.runtime_instance_id, scope.epoch + 1)
    assert not await web_server._ws_client_runtime_authorized(ws, "dashboard.ws.message")
    assert ws.closed["code"] == 4401
```

另加 `registry.clear()` case，期望 close code `4403`、reason `Local capability changed`，不能是
`Ansatz login required`。

- [ ] **Step 2: 运行 WS 测试，确认 red**

```bash
scripts/run_tests.sh tests/hermes_cli/test_dashboard_auth_ws_auth.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py -q
```

Expected: 现有 ticket claim 携带 digest/valid_until，并在 token 到期后关闭 socket。

- [ ] **Step 3: 改造 ticket claim**

`api_auth_ws_ticket()` 对 Desktop grant 调用 `backend_scope_tokens.ws_claim(desktop_grant)`，ticket 中
只放：

```python
{
    "connection_id": grant.connection_id,
    "runtime_instance_id": grant.auth.runtime_instance_id,
    "epoch": grant.auth.epoch,
    "backend_generation": registry.backend_generation,
}
```

不放 registration id、digest 或 bearer expiry。ticket 本身仍 single-use、30 秒 TTL。

- [ ] **Step 4: 改造 upgrade 与每消息授权**

upgrade 消费 ticket 后调用 `authorize_ws_claim()`；成功后把 validated claim 放到 ws state。每条消息
再次调用 `authorize_ws_claim()`，它只检查 backend generation、精确 account scope 和 owner
liveness。正常 token promotion 不改变 generation；control EOF/backend teardown 调用 `clear()`
改变 generation。

catch 顺序：`BackendScopeTokenRejected` → 4403 local reason；`AuthRequired` → 4401 login reason。
Electron 继续在每次建链前用 manager 当前 active token mint 新 ticket，不缓存 ticket。

- [ ] **Step 5: 运行测试并提交**

```bash
scripts/run_tests.sh tests/hermes_cli/test_dashboard_auth_ws_auth.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py tests/hermes_cli/client_auth/test_boundaries.py -q
cd apps/desktop
npx vitest run --project electron electron/local-capability-manager.test.ts electron/backend-json-client.test.ts
cd ../..
git add hermes_cli/client_auth/runtime.py hermes_cli/dashboard_auth/routes.py hermes_cli/web_server.py tests/hermes_cli/test_dashboard_auth_ws_auth.py tests/hermes_cli/client_auth/test_backend_scope_tokens.py apps/desktop/electron/main.ts
git commit -m "fix(auth): bind desktop websockets to scope epochs"
```

Expected: token 轮换/到期不影响已建立 WS；logout/epoch/generation 变化仍即时拒绝。

### Task 9: 在 Python auth owner 中加入 Desktop-local cloud availability

**Files:**
- Modify: `hermes_cli/client_auth/runtime.py`
- Modify: `hermes_cli/client_auth/bridge.py`
- Modify: `tests/hermes_cli/client_auth/test_runtime.py`
- Modify: `tests/hermes_cli/client_auth/test_bridge.py`

- [ ] **Step 1: 写已认证运行中断网和重启断网失败测试**

```python
@pytest.mark.parametrize("owner_factory", [vault_owner_factory, memory_owner_factory])
def test_desktop_continuity_preserves_scope_when_account_server_is_unreachable(owner_factory):
    owner, _backend, client, clock = owner_factory(local_continuity=True)
    active = owner.login("alice", bytearray(b"secret"))
    client.status_error = AuthServiceError("server_unavailable")
    clock.now += 60
    degraded = owner.refresh()
    assert degraded.state is AuthState.AUTHENTICATED
    assert degraded.cloud_state is CloudState.UNREACHABLE
    assert degraded.scope == active.scope
    consumer = owner.connect_consumer(allow_local_continuity=True)
    assert consumer.require_authorized("desktop.local.file", expected=active.scope) == active.scope
    strict = owner.connect_consumer(allow_local_continuity=False)
    with pytest.raises(AuthRequired, match="server_unavailable"):
        strict.require_authorized("cli.tool", expected=active.scope)


def test_vault_owner_restores_local_scope_offline_from_a_valid_secure_record():
    owner, backend, client, clock = vault_owner_factory(local_continuity=True)
    owner.login("alice", bytearray(b"secret"))
    encoded_record = backend.raw
    restarted, _backend2, client2, _clock2 = vault_owner_factory(local_continuity=True)
    restarted._secret_backend.raw = encoded_record
    client2.status_error = AuthServiceError("server_unavailable")
    snapshot = restarted.refresh()
    assert snapshot.state is AuthState.AUTHENTICATED
    assert snapshot.cloud_state is CloudState.UNREACHABLE
    assert snapshot.username == "alice"
    assert snapshot.runtime_instance_id != owner.snapshot().runtime_instance_id
```

另写 tests：首次安装无 record 保持 signed-out；malformed/vault read failure 不开放本地；
`SessionRejected` 删除 record、提升 epoch 并 locked；已知绝对 Session 过期进入
`REAUTH_REQUIRED` 但 Desktop-local consumer 仍可用；`trace_token()` 在两种 degraded state 均拒绝；
logout/revocation tombstone 在 App 离线重启后不能恢复旧 Cookie 或旧 scope。

- [ ] **Step 2: 运行 owner/bridge tests，确认 red**

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py tests/hermes_cli/client_auth/test_bridge.py -q
```

Expected: FAIL；snapshot 没有 `cloud_state`，所有 refresh 网络错误立即 locked。

- [ ] **Step 3: 扩展 snapshot 与 consumer policy**

```python
class CloudState(StrEnum):
    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    REAUTH_REQUIRED = "reauth_required"


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: AuthState
    cloud_state: CloudState | None
    epoch: int
    valid_until: float
    runtime_instance_id: str
    boot_id: str
    username: str | None
    session_expires_at: str | None
    reason: str | None
```

authenticated constructor 设 `ACTIVE`；signed-out/locked 设 `None`。新增
`from_trusted_record()` 与 `with_cloud_state()`，前者只接受 `_decode_cookie_blob()` 已验证的 OS secure
store record，并生成新 runtime instance。

`RuntimeSnapshot.require_authorized()` 增加 keyword-only `allow_local_continuity=False`：

- `ACTIVE`：保持现有 valid_until/boot/scope 检查。
- `UNREACHABLE`/`REAUTH_REQUIRED`：仍检查 state、boot、exact scope 和 owner liveness；只有
  `allow_local_continuity=True` 才跳过远端 lease deadline。
- signed-out/locked：始终拒绝。

把该 flag 传入 `RuntimeConsumer`、`RemoteRuntimeConsumer`、`connect_consumer()` 和 runtime
`authorize` wire request；默认 false，避免改变 CLI/TUI/普通 gateway。

- [ ] **Step 4: 修改 owner refresh 与 Desktop bridge 启用流程**

`_OwnerCore` 增加 `local_continuity_enabled`，默认 false。启用时：

- `server_unavailable`、timeout/TLS/5xx/invalid response/rate limit：保留 record/scope，发布
  `UNREACHABLE`，继续带 jitter 后台 refresh。
- 本地 absolute expiry 已知已过：发布 `REAUTH_REQUIRED`，不删除 record。
- `SessionRejected` 或显式 logout：保持现有 delete/epoch change/locked。
- vault 读取失败、record schema 错误：保持 fail closed。

owner runtime 增加同 OS 用户可调用的精确操作 `enable_desktop_local_continuity`；它不返回 secret，
只切换本 owner policy。`bridge.main()` connect/start owner 后立即调用；旧 owner 不支持时关闭旧连接并
通过现有 recovery 启动当前 runtime owner。Desktop backend 的 `authorize_entrypoint()` 仅在现有
`HERMES_DESKTOP=1` 时创建 `allow_local_continuity=True` consumer。

bridge public result 增加严格 `cloud_state` key，未知值拒绝；bridge protocol version 仍为 1，因为
字段由成对发布的 auth runtime contract 管理，旧 runtime 会在启动前被 probe 拒绝。

把 secure-store blob 升级为向后兼容 union：继续读取已有 version-1 CookieRecord；新写入使用
version 2 的 `{"state":"authenticated", ...cookies...}`。显式 logout 写入不含 Cookie 的
`{"version":2,"state":"signed_out"}` tombstone，权威 `SessionRejected` 写入
`{"version":2,"state":"revoked"}` tombstone。只有成功在线 login 才能覆盖 tombstone 为新的
authenticated record；不能用删除失败后的旧 Cookie 作为离线恢复依据。

- [ ] **Step 5: 运行测试并提交**

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py tests/hermes_cli/client_auth/test_bridge.py tests/hermes_cli/client_auth/test_boundaries.py -q
git add hermes_cli/client_auth/runtime.py hermes_cli/client_auth/bridge.py tests/hermes_cli/client_auth/test_runtime.py tests/hermes_cli/client_auth/test_bridge.py
git commit -m "feat(auth): keep trusted desktop local access offline"
```

Expected: Desktop-local continuity tests通过，严格 consumer 和所有明确撤销 tests 仍 fail closed。

### Task 10: 让 Electron/Renderer 保留降级 scope 且只在真正 locked 时进入登录门

**Files:**
- Modify: `apps/desktop/electron/auth-bridge.ts`
- Modify: `apps/desktop/electron/auth-bridge.test.ts`
- Modify: `apps/desktop/electron/auth-coordinator.ts`
- Modify: `apps/desktop/electron/auth-coordinator.test.ts`
- Modify: `apps/desktop/electron/main.ts`
- Modify: `apps/desktop/src/components/auth-gate.tsx`
- Modify: `apps/desktop/src/components/auth-gate.test.tsx`
- Modify: `apps/desktop/src/global.d.ts`

- [ ] **Step 1: 写 coordinator 降级状态失败测试**

```ts
const cloudUnreachable: BridgeStatus = {
  ...authenticated,
  cloud_state: 'unreachable',
  reason: 'server_unavailable',
  valid_until: 0
}

test('keeps the exact local scope and skips cleanup during cloud outage', async () => {
  const { coordinator, bridge, cleanup } = fixture(authenticated)
  await coordinator.start()
  bridge.status.mockResolvedValue(cloudUnreachable)
  const status = await coordinator.refresh('local')
  assert.equal(status.cloud_state, 'unreachable')
  assert.deepEqual(coordinator.scope('local'), connectionScopeFromStatus(authenticated, 'local'))
  assert.equal(cleanup.mock.calls.length, 0)
  await coordinator.require('local', null)
})

test('cleans once on authoritative lock and ignores late degraded status', async () => {
  const { coordinator, emit, cleanup } = fixture(authenticated)
  await coordinator.start()
  emit({ ...signedOut, state: 'locked', cloud_state: null, epoch: 3, reason: 'session_rejected' })
  assert.equal(cleanup.mock.calls.length, 1)
  assert.equal(coordinator.scope('local'), null)
})
```

增加 bridge tests：strict key set 包含 `cloud_state`；unknown/missing value 拒绝；secret 仍不出 bridge。

- [ ] **Step 2: 写 AuthGate 不闪登录失败测试并运行 red**

```tsx
it('keeps the protected tree mounted while cloud auth is unreachable', async () => {
  const mounted = vi.fn()
  const { emit } = renderGate(
    { status: vi.fn(async () => authenticated) },
    null,
    <ProtectedMountProbe onMount={mounted} />
  )
  expect(await screen.findByText('Protected Hermes application')).not.toBeNull()
  act(() => emit({ ...authenticated, cloud_state: 'unreachable', reason: 'server_unavailable', valid_until: 0 }))
  expect(screen.getByText('Protected Hermes application')).not.toBeNull()
  expect(screen.queryByRole('heading', { name: 'Sign in to Ansatz' })).toBeNull()
  expect(mounted).toHaveBeenCalledTimes(1)
})
```

Run:

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-bridge.test.ts electron/auth-coordinator.test.ts
npx vitest run --project ui src/components/auth-gate.test.tsx
```

Expected: FAIL；类型没有 cloud state，coordinator/renderer 仍把超时映射为 locked。

- [ ] **Step 3: 实现类型与 coordinator transition**

所有 Bridge/Renderer status 增加：

```ts
cloud_state: 'active' | 'unreachable' | 'reauth_required' | null
```

`connectionScopeFromStatus()` 对三种 authenticated cloud state 都返回 exact scope。
`AuthCoordinator.applyStatus()` 只在 state 非 authenticated、epoch/runtime instance 改变时删除 scope
和 cleanup。`requireConnection()` 对 local degraded status 不使用 `valid_until` 触发锁定；remote
connection 保持现有在线授权规则。

`applyFailure()` 在 local 之前已 authenticated 且 reason 为 `server_unavailable` 或 auth bridge
`runtime_unavailable` 时，保留 scope，发布一个 `cloud_state: 'unreachable'` copy，并启动现有
bridge recovery；没有 prior scope 时仍返回 locked/checking，不能凭空开放本地。

- [ ] **Step 4: 保持 Renderer tree 与 Main runtime gate 稳定**

`desktopStatusForRenderer()` 透传 cloud state；`startDesktopAuthRuntime()` 对 authenticated degraded
仍启用 capability shell/start local backend。`cleanupDesktopCapabilities()` 只由 true signed-out/
locked/scope change 调用。

AuthGate 的 auth.status deadline 若当前 status 已 authenticated，只保留当前 tree 并等待 Main
event；初次 checking 超时仍显示登录/Retry。`DesktopAuthContext` 暴露 cloud state 给 Trace 等云端
功能决定自身不可用，但不展示全局 toast、banner 或轮换进度。

- [ ] **Step 5: 运行测试并提交**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-bridge.test.ts electron/auth-coordinator.test.ts electron/desktop-runtime-gate.test.ts electron/local-backend-capability.test.ts
npx vitest run --project ui src/components/auth-gate.test.tsx src/components/task4-auth-ui.contract.test.tsx
npm run typecheck
git add electron/auth-bridge.ts electron/auth-bridge.test.ts electron/auth-coordinator.ts electron/auth-coordinator.test.ts electron/main.ts src/components/auth-gate.tsx src/components/auth-gate.test.tsx src/global.d.ts
git commit -m "fix(desktop): keep local UI active through cloud outages"
```

Expected: 网络/bridge 暂时失败不卸载 protected tree；显式 logout 与 session rejection 仍立即卸载。

### Task 11: 加入跨进程故障注入、runtime 配对和发布验收

**Files:**
- Create: `tests/fixtures/desktop_scope_control_backend.py`
- Create: `apps/desktop/electron/local-capability-integration.test.ts`
- Modify: `apps/desktop/electron/auth-runtime-contract.ts`
- Modify: `apps/desktop/electron/auth-runtime-contract.test.ts`
- Modify: `apps/desktop/electron/package-runtime/windows-auth-toolchain.test.ts`
- Modify: `apps/desktop/e2e/installed-windows-auth.spec.ts`
- Modify: `scripts/install.sh`
- Modify: `scripts/install.ps1`

- [ ] **Step 1: 创建真实 Python fixture 与失败的 Node 集成测试**

fixture 必须 import 产品的 protocol/registry/control reader，并只提供六个测试 endpoint：candidate
probe、active status、计数配置 PUT、模拟模型 POST、Trace 上传 POST 和 WS echo。命令行参数控制 ACK delay/drop/duplicate/order；stdout 普通日志中
放一个 sentinel，测试确认 control line 被过滤。它不能复制 register/promote 状态机。

Node test 使用真实 `BackendControlChannel`、`LocalCapabilityManager` 和 `backend-json-client`：

```ts
test('survives three real Node-Python rotations while writes and sockets stay live', async () => {
  const fixture = await startPythonScopeFixture({ ackDelayMs: 500 })
  try {
    const manager = createRealManager(fixture)
    await manager.activate(fixture.binding)
    const writes = Array.from({ length: 40 }, (_, index) => fixture.putConfig({ model: `model-${index}` }))
    const modelRequests = Array.from({ length: 40 }, (_, index) => fixture.requestModel(`prompt-${index}`))
    const traceUploads = Array.from({ length: 40 }, (_, index) => fixture.uploadTrace({ span: index }))
    for (let index = 0; index < 3; index += 1) {
      await manager.refresh(fixture.binding.key, 'recovery')
      await fixture.sendWsMessage(`rotation-${index}`)
    }
    await Promise.all([...writes, ...modelRequests, ...traceUploads])
    assert.equal(await fixture.writeCount(), 40)
    assert.equal(await fixture.modelRequestCount(), 40)
    assert.equal(await fixture.traceUploadCount(), 40)
    assert.deepEqual(await fixture.wsMessages(), ['rotation-0', 'rotation-1', 'rotation-2'])
    assert.equal(fixture.logsContaining('Ansatz login required').length, 0)
    assert.equal(fixture.logsContaining(fixture.anyIssuedBearer()).length, 0)
  } finally {
    await fixture.close()
  }
})
```

另建 cases：首 ACK 丢失、duplicate/out-of-order ACK、5 秒 ACK delay、promotion 后 ACK 丢失、
logout during overlap、scope switch、provider 401、backend process exit。

- [ ] **Step 2: 运行集成测试，确认 red，再完成 fixture**

```bash
cd apps/desktop
npx vitest run --project electron --no-file-parallelism electron/local-capability-integration.test.ts
```

Expected before fixture completion: FAIL；Python process/endpoint 尚不可用。Expected after completion:
所有 fault cases PASS，且无 bearer/login 文案泄漏。

- [ ] **Step 3: 把 scope protocol v2 加入 runtime contract probe**

`authRuntimeProbeSnippet()` 同时执行：

```python
import hermes_cli.client_auth.bridge as bridge
from hermes_cli.client_auth.backend_scope_protocol import DESKTOP_SCOPE_PROTOCOL_VERSION
assert bridge.PROTOCOL_VERSION == 1
assert DESKTOP_SCOPE_PROTOCOL_VERSION == 2
```

旧 runtime import/值不匹配时 `validateAuthRuntimeContract()` 返回
`scope_protocol_mismatch`，走现有 verified bootstrap/update/repair。安装脚本在写 completion marker 前
执行同一 import probe；不增加 marker secret 或用户配置。

- [ ] **Step 4: 增加 package/installed E2E contract**

Windows auth toolchain fixture 和 installed Windows E2E 断言：

- 捆绑源码包含 `backend_scope_protocol.py`；
- auth/full venv probe 报 v2；
- ready file/stdout 报 `desktop_scope_protocol=2`；
- v1 fixture 进入 local runtime repair，不显示登录错误；
- App 重启 + account server fixture 不可达时恢复 authenticated/unreachable 本地 UI；
- explicit logout 后重启保持 signed-out；
- renderer diagnostics、安装日志和 crash fixture 无 bearer。

macOS 对应行为由通用 Electron/Python tests 加 DMG smoke 覆盖；Windows-only 安装行为使用
`@pytest.mark.windows_only`/Playwright Windows lane，不伪造 host OS。

- [ ] **Step 5: 运行完整验证矩阵**

Python：

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_backend_scope_tokens.py tests/hermes_cli/client_auth/test_runtime.py tests/hermes_cli/client_auth/test_bridge.py tests/hermes_cli/client_auth/test_boundaries.py tests/hermes_cli/test_dashboard_auth_ws_auth.py tests/gateway/test_api_server.py -q
```

Desktop focused：

```bash
cd apps/desktop
npx vitest run --project electron --no-file-parallelism electron/auth-scope-token.test.ts electron/backend-control-channel.test.ts electron/local-capability-manager.test.ts electron/local-backend-capability.test.ts electron/backend-json-client.test.ts electron/local-capability-integration.test.ts electron/auth-bridge.test.ts electron/auth-coordinator.test.ts electron/auth-runtime-contract.test.ts electron/package-runtime/windows-auth-toolchain.test.ts
npx vitest run --project ui src/components/auth-gate.test.tsx
npm run typecheck
npm run lint
```

Desktop full：

```bash
npm run test:desktop:platforms
npm run test:desktop:all
```

Repository hygiene：

```bash
cd ../..
git diff --check
git status --short
```

Expected: 所有命令 exit 0；`git status` 只显示本实现修改和用户原有
`docs/design/object-context-memory-improvements.md`，不出现 token/log fixture 产物。

- [ ] **Step 6: 在原生 package lane 验证并提交**

macOS runner：

```bash
cd apps/desktop
npm run dist:mac:dmg
npm run test:desktop:dmg
```

Windows runner：

```powershell
cd apps/desktop
npm run dist:win:nsis
npm run test:desktop:nsis
```

两端均须完成：首次在线登录、三次强制轮换、配置写入、WS、provider 401、账户服务断开/恢复、
显式 logout。正常轮换期间 `Ansatz login required` 出现次数必须为 0。

最后提交：

```bash
git add tests/fixtures/desktop_scope_control_backend.py apps/desktop/electron/local-capability-integration.test.ts apps/desktop/electron/auth-runtime-contract.ts apps/desktop/electron/auth-runtime-contract.test.ts apps/desktop/electron/package-runtime/windows-auth-toolchain.test.ts apps/desktop/e2e/installed-windows-auth.spec.ts scripts/install.sh scripts/install.ps1
git commit -m "test(desktop): verify seamless scope rotation end to end"
```

## 完成判据映射

| 规范要求 | 实施任务 |
| --- | --- |
| 30m TTL / 20m rotate / 60s overlap | Tasks 1, 2, 5 |
| ACK + probe + promote + atomic switch | Tasks 2, 3, 4, 5 |
| 前台请求不负责正常轮换 | Tasks 5, 6 |
| HTTP、配置、模型请求、Trace 与 WS 在轮换中连续 | Tasks 7, 8, 11 |
| 非幂等写入恰好一次 | Task 7, Task 11 |
| token/backend 错误不显示登录 | Tasks 3, 7, 10 |
| provider 401 保持 provider error | Tasks 3, 7, 11 |
| WS 不受 bearer 轮换影响 | Task 8 |
| logout/epoch/backend generation 即时撤销 | Tasks 2, 6, 8 |
| 账户服务不可达时本地继续 | Tasks 9, 10, 11 |
| App 重启离线恢复可信本地身份 | Tasks 9, 10, 11 |
| v1/v2 mismatch 进入 runtime repair | Tasks 4, 6, 11 |
| bearer 不进 Renderer/URL/env/log/磁盘 | Tasks 1, 4, 5, 11 |
| macOS/Windows 捆绑 runtime 成对发布 | Task 11 |

实现完成后不要仅凭单元测试宣称修复；必须以 Task 11 的跨进程故障注入和两端 package lane 作为
最终验收证据。
