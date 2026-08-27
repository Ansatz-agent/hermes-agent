# Desktop Trace 连续耐久化与 macOS 发布签名设计

## 状态

已确认，等待实施计划。

本设计修复 Ansatz Desktop 中 Trace 本地入口仍然存在、但实际
forwarder 已停止监听后，新 Trace 无法进入加密 outbox 的生命周期缺陷。
目标不是让失败“更少发生”，而是建立如下合同：只要设备上的可信账户
scope 仍然有效且本地安全存储可写，Trace 就必须先可靠地加密落盘；
Trace 服务、网络或 auth bridge 不可达只能暂停上传，不能暂停接收。

文档同时记录 macOS 反复弹出“Ansatz Safe Storage”登录钥匙串密码的独立
根因和发布门槛。它不是 Trace 生命周期缺陷，也不是 macOS“隐私与安全性”
权限。

## 决策摘要

- 采用账户级稳定耐久接收器，将“本地接收并加密落盘”与“获取云端凭据并
  上传”拆成两个独立生命周期。
- 本地 ingress 和 outbox 只绑定可信账户，不绑定短期 Trace 上传 token。
- 每个 Trace batch 只有在本地 durable commit 完成，或服务端返回经校验的
  receipt 后，才向生产端返回成功。
- 同一账户的 Session、auth owner 或上传 token 更新只原子更新 uploader 的授权
  上下文；不重启 ingress，不关闭 outbox，不变更本地 endpoint 或 bearer。
- 只有显式 logout、权威账户撤销或账户切换才关闭当前账户的新 Trace 接收并
  旋转本地 bearer。
- 保留已有加密、journal 恢复、receipt、去重、quarantine、容量回收、namespace
  迁移和终止撤销能力，不用简化重写取代它们。
- 保持现有 Trace 服务 receipt、幂等 batch ID 和终止撤销响应协议；本修复
  不要求服务端改动。
- macOS 正式发布必须使用稳定 Developer ID Application 身份签名并公证。
  ad-hoc 签名仅允许用于明确的本地开发产物。

## 已验证的现象与根因

### 运行时证据

故障发生时，模型请求仍然成功，但 OpenTelemetry BatchSpanProcessor 连续报告
`HTTP export failed: network error`。进程中只剩一个 Ansatz TCP listener，即稳定
Trace façade；实际 forwarder listener 已不存在。当时 outbox journal 没有新记录，
说明 Trace 连本地耐久化边界都没有到达，无法在稍后自动补传。

受控重启后，第二个 listener 恢复，新模型请求的 Trace 获得
`outcome: accepted` 的服务端 receipt。这证明服务端 Trace ingest 正常，故障在
Desktop 客户端生命周期。

### 代码级根因

当前 `ensureDesktopTraceForwarder()` 在确认新 owner/status 可用之前，就会：

1. 清空全局 forwarder、lifecycle、store 和 context。
2. 将稳定 façade 与当前 delegate 分离。
3. 停止之前可用的 forwarder 并关闭 outbox writer。
4. 然后才读取 auth coordinator status 并验证 owner。

如果 auth bridge/status 此时只是短暂缺失或不一致，启动路径抛出
`AuthBridgeError('auth_required', 'session_rejected')`。`TraceRuntimeStartupRecovery` 又将所有
`AuthBridgeError` 视为不可恢复，状态回到 `idle` 且不安排 retry。结果是 façade 仍在，
但只会向 Trace 生产端返回 503，而 OTel 生产端不会将这些数据写入 Ansatz
outbox。

只把 `AuthBridgeError` 改为可重试能够让系统稍后恢复，但重试窗口中仍会丢
Trace，不满足连续耐久化要求。

## 范围与非目标

### 本次范围

- Electron Main 中 Trace ingress、outbox、uploader 和 auth owner 更新的生命周期。
- Desktop 本地 backend 获取稳定 Trace transport descriptor 的现有路径。
- 当地加密 outbox 的启动恢复、持续接收和补传触发。
- 本地诊断事件、行为测试、打包验收和 macOS release 签名门槛。

### 非目标

- 不改变 Trace ingest 服务、receipt 格式、账户数据库或远端 Session 协议。
- 不改变模型请求、模型配置、会话、terminal、文件或 WebSocket 的业务协议。
- 不绕过 macOS Keychain，不将 outbox data key、Cookie 或 token 改存为明文。
- 不将本地 Trace 保留扩展成无限磁盘承诺。已有 2 GiB 容量上限和 30 天保留
  期继续生效。

## 与本地 scope token 无感轮换的关系

本设计建立在
`2026-08-27-desktop-local-scope-token-rotation-design.md` 已确认的账户语义上：

- `ACTIVE`、`CLOUD_UNREACHABLE` 和 `CLOUD_REAUTH_REQUIRED` 都仍是可信本地账户
  scope，必须允许 Trace 本地接收。
- `UNAUTHENTICATED` 表示从未建立可信账户或本地记录不可用，不得开启受保护
  Trace ingress。
- `LOCKED` 只由显式 logout、权威账户 scope 撤销或账户切换产生，必须立即停止
  新 Trace 接收。
- 本地 capability token 轮换不得重启 Trace ingress 或 uploader，也不得映射为登录
  状态。

## 备选方案与决策

### 方案一：账户级稳定耐久接收器（采用）

本地接收、加密落盘和云端上传使用不同生命周期。同账户 auth 更新只换 uploader
授权上下文，不停 ingress/store。这是唯一能同时覆盖运行时与冷启动不可达
场景的方案。

### 方案二：双 forwarder 分阶段切换（不采用）

保留旧 forwarder，候选 forwarder 完全启动后再切换 façade。但两个 forwarder 不能
安全地同时持有同一 outbox writer，冷启动时又没有旧 forwarder 可以托底，因此
仍存在丢失窗口。

### 方案三：只修改错误分类和重试（不采用）

将临时 `AuthBridgeError` 设为可恢复，并尽量延后关闭旧 forwarder。改动最小，
但重试窗口中 façade 仍可能返回 503，不能给出连续耐久化保证。

## 目标架构

### 权威边界

- auth coordinator 是当前可信账户、账户状态和终止撤销的权威来源。
- Trace durability runtime 是当前账户的 ingress、outbox writer 和 uploader 生命周期的
  唯一写入者。
- Trace outbox store 是本地 batch 耐久性、receipt、去重、容量与 quarantine 状态的
  权威来源。
- uploader 拥有上传凭据和上游重试状态，但不拥有 ingress 开关或账户登录状态。
- Renderer 不获取 outbox key、Trace token、本地 bearer 或内部轮换状态。

### TraceDurabilityRuntime

Electron Main 引入狭的 `TraceDurabilityRuntime`，避免继续将 Trace 状态扩散到
`main.ts`。它按当前账户保存：

- 账户级 lifecycle generation。
- 当前 `accountKey` 和最新 `TraceOwner` snapshot。
- 稳定 ingress endpoint 与本地 bearer。
- 唯一已打开的 account-bound outbox writer。
- 可独立更换的 upload authorization generation 和 credential provider。
- 单飞 uploader pump、定时器、退避时间和运行中上游请求。

它对 Main 暴露类似以下的行为，具体名称可在实施计划中细化：

- `activate(trustedAccount)`：打开或恢复当前账户 outbox，开启 ingress，然后异步开启
  uploader。
- `rebindSameAccount(owner)`：原子更新后续新 batch 的 owner 与 uploader 授权上下文，
  不停 ingress/store。
- `triggerRecovery(reason)`：聚合网络恢复、resume、focus、timer、新 batch 和 token-ready
  信号，触发单飞 pump。
- `lock(reason)`：在终止账户事件下同步关闭 admission、旋转 bearer、失效上传
  generation，然后收敛运行中任务并关闭 store。
- `stop()`：仅用于 App 关闭或完整 Trace runtime 销毁。

### 本地 ingress 与 durable admission

ingress 保留现有 loopback-only、32-byte 随机 bearer、OTLP content type、body 上限和
metadata 验证。它不读 auth bridge，也不获取云端 token。

对每个合法 batch：

1. 捕获当前 account generation 和 owner snapshot。
2. 启动现有 durable local commit。
3. 在没有 backlog 且 uploader 当前可用时，保留直接上传与本地 commit 竞速优化。
4. 任一路先取得可验证的 durable ownership 后才返回成功。
5. 如果两路都失败，返回本地耐久性错误，不得假报成功。

同一账户 owner 更新不改变 account key，因此继续使用同一 account-bound store。
更新后接收的 batch 携带新 owner snapshot；更新前已耐久化的 batch 保持原始记录，
但可使用同账户当前有效上传凭据补传。

### uploader 与授权 snapshot

uploader 从 store 读取 pending batch。每次上传捕获一个不可变授权 snapshot，至少
包含 account key、account/session identity、authorization generation 和 credential。

- 在任何 `await` 后发布 receipt、quarantine 或终止撤销前，重新验证 snapshot 仍属于
  当前 account generation。
- 401 使当前 credential 失效并尝试一次强制刷新。刷新失败只延后 pump，
  不影响 ingress。
- 403 只有在响应包含经严格验证的终止撤销 payload，且其 account/session
  与该次上传的授权 snapshot 一致时，才通知 auth coordinator 锁定。
- 上传返回 400、409、413 或 415 等明确 batch 级终止错误时，只 quarantine 该
  batch，不改变账户或 ingress 状态。
- 网络、DNS、TLS、timeout、429、5xx、无效 receipt、普通 403 和凭据暂时不可用均
  保留 batch，按现有带 jitter 退避重试。

### 账户生命周期

#### 冷启动

1. auth coordinator 恢复可信本地账户记录。
2. durability runtime 使用该本地 owner 打开并恢复 outbox journal。
3. ingress 就绪后再将 Trace transport descriptor 附加到本地 backend。
4. uploader 异步获取 Trace credential；认证桥或 Trace 服务不可达不阻塞本地
   backend 和 ingress 就绪。

从未登录、可信记录缺失/损坏或本地 logout tombstone 已撤销记录时，不开启受
保护 ingress。

#### 同账户 owner/Session 更新

1. 确认 account key 未变。
2. 提升 upload authorization generation。
3. 原子发布新 owner snapshot 和新 credential source。
4. 使旧授权 snapshot 的运行中上传结果不能再改变当前状态。
5. 立即触发新 generation 的 pump。

整个过程不停 ingress、不关 store、不改变 endpoint/bearer，不向本地 backend
重发 transport descriptor。

#### logout、权威撤销与账户切换

1. 提升 account generation 并同步关闭新 admission。
2. 旋转本地 bearer，使所有旧生产端 descriptor 失效。
3. 中止上游请求，停止 recovery timer，等待已进入 durable admission 的有界本地
   commit 收敛。
4. 关闭 store writer。已落盘数据保持加密，不用新账户凭据上传。
5. 若以后同一账户重新获得授权，可重新打开该账户 namespace 继续补传。

## 状态与故障语义

durability runtime 的内部状态不映射为全局登录布尔值：

| 状态 | 本地接收 | 上传 | UI/恢复 |
| --- | --- | --- | --- |
| `detached` | 拒绝 | 停止 | 仅用于未认证或尚未激活 |
| `starting` | 等待 outbox 恢复 | 停止 | 本地 Trace 启动，不是登录错误 |
| `ready` | 加密耐久化 | 正常 | 无前台状态 |
| `upload_degraded` | 加密耐久化 | 退避暂停 | 静默恢复，只写本地诊断 |
| `storage_failed` | 不得假报成功 | 可继续尝试已落盘数据 | 本地安全存储故障，非登录 |
| `locked` | 拒绝并失效 bearer | 停止 | 仅由权威账户锁定进入 |
| `stopping` | 拒绝 | 收敛中 | App 关闭或账户硬切换 |

故障分类固定为：

- Trace 服务、网络、auth bridge、Trace credential 或 receipt 暂时不可用：
  `upload_degraded`。
- 磁盘满、写入失败、Safe Storage 不可用、无法恢复的完整性故障：
  `storage_failed`。
- 显式 logout、严格验证的账户/会话撤销、账户切换：`locked`。

除最后一类外，均不得导航到登录页、清理可信账户或显示
`Ansatz login required`。

## 必须保留的现有能力

实施是生命周期拆分，不是 Trace 简化重写。以下合同全部保留：

- Safe Storage 包装 data key，且 key 与 account key 绑定。
- journal group commit、崩溃恢复、torn-tail 修复、完整性 quarantine 和 key-loss
  quarantine。
- 2 GiB 容量上限、30 天保留期、容量回收、空闲 compaction 和流式会话期间的
  compaction 避让。
- 64 MiB receipt tombstone 上限、100,000 receipt 数量上限、batch 幂等和重复检测。
- 大 OTLP payload 拆分、超大 span quarantine、content type、body 上限和 loopback 边界。
- 可信 legacy namespace 向 account namespace 的迁移屏障，以及迁移完成前不上传。
- 经校验 gateway receipt 才能取消本地 commit 或确认 pending batch。
- 上游单飞 pump、周期恢复、focus/resume/online 信号、token 提前刷新和带 jitter
  退避。
- 运行中上游请求的 generation 防护，以及停止时的有界本地 admission 收敛。
- 稳定 Trace transport 自动附加到已运行本地 backend，并在 backend 重启后自动重试。

非 Trace 功能不应因本次拆分而改变代码路径。模型请求、模型配置、会话、
WebSocket、terminal、文件和本地 scope token 轮换必须通过现有回归。

## 存储与可用性边界

“一直可以传”的精确合同是：在本地安全存储可用且未超出已声明保留政策时，
上游不可达不能阻止新 Trace 加密落盘，且恢复后自动补传。

- 容量和保留仍由 outbox 权威实现。容量压力下的旧数据回收必须进入 diagnostics。
- 如果本地 commit 失败且云端也未给出有效 receipt，ingress 必须失败，不能返回
  200。
- Safe Storage 被锁定或用户拒绝访问时，不得降级为明文 outbox 或可预测密钥。
- 存储故障是本地耐久性故障，不是账户退出。

## 可观测性与秘密处理

仅增加本地低噪声结构化诊断，不新增 outbound telemetry：

- `trace_admission_ready`
- `trace_owner_rebound`
- `trace_upload_degraded`
- `trace_upload_recovered`
- `trace_storage_failed`
- `trace_terminal_locked`
- `trace_backlog_recovered`

诊断可包含 account generation、upload generation、失败类别、退避次数、pending count/bytes
和耗时。不得包含 bearer、access token、Cookie、wrapped/data key、用户名、payload、
完整 account/session id 或可重放的关联标识。

同一持续故障只记录状态转换和有界摘要，不在每个 30 秒 timer 上重复刷屏。

## macOS 登录钥匙串弹窗与发布签名

### 根因

Electron `safeStorage` 将 Ansatz 的本地秘密保存在 macOS 登录钥匙串的
`Ansatz Safe Storage` 项中。系统弹窗要求的是“登录”钥匙串密码，通常就是 Mac
用户登录密码；这不属于“隐私与安全性”中的相机、文件、辅助功能等权限。

当前本地包是 ad-hoc 签名，没有 TeamIdentifier。它的 designated requirement 由当前
二进制 cdhash 构成，所以每次重新打包后都可被 Keychain 视为新的访问者。即使用户
已经对上一个构建点击“始终允许”，新构建仍可再次要求密码。

### 安全解法

- 保留 Safe Storage 和 Keychain 安全边界。
- macOS 正式包使用同一 Developer ID Application 身份、稳定 bundle ID、hardened
  runtime 和经审核 entitlements 签名。
- 向 Apple 提交 notarization，并对最终发布产物 staple/validate。
- 正式 release 路径增加产物门槛：ad-hoc、缺失 TeamIdentifier、签名无效、未通过
  notarization/staple 验证的产物不得标记或上传为正式发布。
- 本地开发包可以 ad-hoc，但必须明确为开发产物，不声称可验收“不再弹钥匙串密码”。

当前开发机没有可用 Developer ID Application 身份，因此可完成 Trace 修复、测试和
ad-hoc 打包验证，但最终不再弹窗的生产验收必须使用安装了正式证书的构建机
或 CI 产生的已签名、已公证包。

## 测试设计

实施使用 TDD：每个行为先用当前代码必然失败的测试复现，确认红色失败原因
与预期一致后，再写最小实现。

### 单元与状态机测试

- 已就绪 ingress 之后 auth bridge 短暂失败：新 Trace 仍 durable commit，endpoint/bearer
  不变，无 503。
- 同账户 owner/session 更新：不重开 listener/store，旧上传结果受 generation
  防护，新 batch 使用新 owner。
- 100 个并发 recovery 信号只运行一个 pump；运行中触发聚合为有界 rerun。
- 401 刷新、刷新失败、普通 403、有效/无效终止撤销、过期授权 snapshot。
- logout/撤销/账户切换与正在进行的 admission、upload、receipt 和 timer 竞争。
- 磁盘满、Safe Storage 不可用、commit 失败、损坏 journal 和 key loss 的错误分类。

### 集成与耐久性测试

- 真实 loopback ingress + 真实临时 outbox，上游和 credential source 通过可控 harness
  注入故障。
- Trace 服务断开期间连续发送 batch，跨越多次 App runtime 重启，恢复后每个 batch
  只产生一个 accepted/duplicate receipt。
- auth bridge 不可达期间连续接收，恢复后无人工操作自动补传。
- 冷启动时有可信本地账户但 bridge 不可达：恢复 outbox 并接收新 Trace。
- 同账户 Session 更新与并发 Trace 期间无连接重建、无丢失、无重复。
- 账户 A 切换到账户 B 后，A 的 key、batch、receipt、timer 和运行中结果不得影响 B。

### 回归、打包与安装验收

- 保留现有 TraceForwarder、outbox store/crypto/journal/record/types、recovery、legacy
  migration 和 continuity 测试的行为合同。
- 涉及本次生命周期的源码文本匹配测试改为直接运行真实模块行为的测试。
- 使用项目约束的 Node 26 运行定向 Vitest、Desktop 全量测试、typecheck、lint 和
  打包测试。
- 重新生成 macOS DMG，执行完全卸载/安装，验证模型请求成功、Trace 先本地
  耐久化，然后获得服务端 receipt。
- 故障注入下无 `Ansatz login required`，无 OTel 因 façade 无 delegate 而产生的
  丢失窗口。
- 正式 macOS release 额外验证 Developer ID identity、TeamIdentifier、codesign 有效性、
  notarization 和 stapled ticket。

## 实施分层与预期文件边界

详细计划可以调整文件名，但必须保持以下职责分层：

- 新的 Trace durability runtime/supervisor 模块：账户状态机、同账户 rebind、硬锁定、
  ingress/store/uploader 协调。
- 从现有 `trace-forwarder.ts` 拆出或抽取可独立生命周期的 durable admission 和
  upload pump，复用现有解析、竞速、receipt 和退避逻辑。
- 复用 `trace-outbox-store.ts` 及 crypto/journal/record/types；除为同账户 owner 更新提供
  必要的窄行为外，不重写存储引擎。
- `main.ts` 只保留 auth coordinator 事件、backend transport 和 App lifecycle 的窄接线。
- 发布配置/校验脚本区分本地 dev artifact 与正式 signed/notarized artifact。

## 完成标准

实现只有在以下条件同时满足时才算完成：

1. 有可信本地账户且本地安全存储可用时，Trace 服务、网络或 auth bridge 不可达
   不会阻止新 Trace 加密落盘。
2. 同账户 Session/token/owner 更新不改变 Trace endpoint、本地 bearer、outbox writer 或
   backend transport，且无丢失/重复。
3. 恢复上游后无人工操作自动补传，每个 batch 只得到 accepted 或 duplicate receipt。
4. 只有显式 logout、经验证的权威撤销或账户切换停止当前账户 ingress。
5. 账户、安装实例、outbox key、pending batch、receipt、timer 和延迟异步结果均严格隔离。
6. 磁盘/Safe Storage/完整性故障不假报成功、不明文降级、不显示登录错误。
7. 现有加密、journal、receipt、去重、quarantine、容量/保留、迁移、撤销和恢复合同
   全部通过回归。
8. 模型请求、模型配置、会话、WebSocket、terminal、文件和本地 scope token 轮换不受破坏。
9. 故障注入和真实安装验收中不出现由 Trace 或本地存储错误产生的
   `Ansatz login required`。
10. 正式 macOS release 无法在 ad-hoc/缺失 TeamIdentifier/未公证的状态下成功发布。
11. 使用 Node 26 的定向、全量、集成、typecheck、lint、打包和安装验收全部通过。
