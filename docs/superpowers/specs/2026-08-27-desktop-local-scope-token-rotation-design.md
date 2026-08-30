# Desktop 本地能力令牌无感轮换设计

## 状态

已确认，等待实施计划。

本设计修复 Ansatz Desktop 中本地 backend scope token 轮换期间偶发的
`Ansatz login required`。最终目标不是降低错误出现频率，而是建立一个令牌更新对用户
完全无感、服务端临时不可达时本地能力仍然可用、同时保留显式撤销和账户隔离的客户端
协议。

## 决策摘要

- 本地 scope token 仍是 256-bit 随机 bearer，并精确绑定
  `connection_id + runtime_instance_id + epoch`。
- token 安全寿命为 30 分钟；Electron 在第 20 分钟开始后台轮换，新旧 token 在切换后
  最多重叠 60 秒。
- 轮换使用本地控制协议 v2：注册候选 token、等待 backend ACK、执行本地无副作用探测、
  提升候选 token，最后原子切换 Electron 的活动 token。
- 前台 HTTP、模型配置、Trace、WebSocket 和用户操作不等待轮换，也不负责触发正常轮换。
- 本地 token 错误、backend 故障、账户服务不可达和真正的账户锁定是四类不同状态；只有
  显式退出或权威账户 scope 撤销可以进入登录门。
- 远端账户服务的 API、数据库和部署不在本次改动范围。需要修改 Electron Main、捆绑的
  本地 Python backend，以及少量 Renderer 错误分类和状态呈现。
- 不做临时热修或协议 v1 补丁；直接实现、测试并随同捆绑 runtime 发布协议 v2。

## 背景与已确认根因

当前 Desktop 为每个本地 backend 签发最多 60 秒的 scope token。Electron 在前台请求即将
发生时检查剩余寿命；当只剩 10 秒时，它向 backend 的 stdin 写入新 token，然后在 Node
`stdin.write()` 回调成功后立即把新 bearer 放进请求。

该回调只证明字节进入操作系统管道，不证明 Python 控制线程已经解析、校验并注册了 token。
因此存在以下时序窗口：

1. Electron 生成新 token。
2. `stdin.write()` 完成，Electron 立即把新 token 设为当前 token。
3. 前台请求携带新 token 到达 backend。
4. Python 注册线程尚未把 token 放入 registry。
5. backend 在业务 handler 之前返回本地 401。
6. 公共中间件把所有 `AuthRequired` 都渲染为 `Ansatz login required`。
7. 数毫秒后 token 注册完成，下一次请求恢复正常。

这解释了日志中的三个特征：401 短暂出现后自动恢复、模型请求本身成功、Trace 仍能正常上传。
模型配置流程更容易暴露此问题，是因为保存、切换、刷新会在很短时间内连续发出多个请求；
它不是模型服务端导致的登出。

另外观察到的 provider 错误，例如第三方 API key 无效或 base URL 配置错误，属于 provider
自身的 401/配置错误，必须继续作为 provider 错误呈现，不能进入 Ansatz 登录流程。

## 目标

### 用户体验目标

1. 用户在正常使用期间看不到 token 更新：没有登录页、toast、spinner、页面闪烁、请求丢失
   或 WebSocket 重连。
2. 模型配置、会话、文件、terminal、第三方模型调用和 Trace 不因本地 token 轮换中断。
3. 账户服务暂时不可达只影响必须访问该服务的云端功能；已获授权的本地功能继续工作。
4. 只有显式 logout 或账户服务给出的权威账户 scope 撤销才关闭本地能力并显示登录门。

### 安全目标

1. Renderer 永远拿不到 Django Cookie、CSRF、vault 内容或 token 注册控制权。
2. 本地 backend 只接受为当前连接、当前 runtime instance 和当前 epoch 签发的 bearer。
3. logout、账户切换、owner 换代和权威撤销立即使旧 scope、旧 token 和旧 WebSocket 失效。
4. token 不进入 URL、环境变量、持久化文件、普通日志或 outbound telemetry。
5. token 轮换故障不能扩大权限，也不能把一个连接的授权带到另一个连接或 profile。

## 非目标

- 不改变远端账户服务的登录、Session、数据库或管理员账户流程。
- 不改变第三方 provider 的鉴权方式和错误语义。
- 不重做登录 UI、模型配置 UI、Trace 协议或 Desktop connection/profile 模型。
- 不给未登录用户开放本地 backend，也不弱化显式 logout 和账户切换后的 fail-closed 行为。
- 不引入长期持久化 bearer、设备万能 token、明文 Cookie 或环境变量 secret。
- 不为协议 v1 制作紧急轮换补丁；版本不匹配走本地 runtime 更新/修复状态，而不是登录状态。

## 与既有认证规范的关系

本设计只覆盖 Ansatz Desktop 和由它启动的本地 backend。独立 CLI、Ink TUI、远端 SSH
backend、cloud connection、Docker 和普通 gateway 的认证策略不在本次范围。

以下既有规则继续有效：

- 新用户或没有可信本地账户记录的安装必须先在线登录。
- 受保护 runtime 在从未建立过可信账户身份时不能启动。
- scope 必须精确比较 `runtime_instance_id + epoch`，不能只比较 epoch。
- logout 和账户切换先撤销 scope，再停止或隔离受保护工作。
- bearer 保持高熵、内存态、不可伪造，scope 元数据本身不能作为 bearer。

本设计有意替代旧规范中仅针对 Desktop 本地已认证会话的三项行为：

1. 本地 backend bearer 的最大寿命从 60 秒改为 30 分钟，并增加显式后台轮换协议；短 TTL
   不再承担账户撤销职责。
2. 账户服务网络失败、超时、TLS/5xx、malformed response 或 auth bridge 临时恢复失败不再
   自动等价为 logout。它们进入 `CLOUD_UNREACHABLE`，保留最近一次已验证的本地 scope。
3. App 重启时，已有可信 secure-store 账户记录可以在在线校验不可达时恢复为本地降级态；
   “恢复 Session 必须先在线验证”仍适用于云端能力和首次登录，不再阻止这些本地能力。

因此，旧文档中“任何网络错误立即锁定”以及“60 秒是显式撤销检测窗口”的表述，不再适用于
Desktop 本地已建立的可信会话。显式撤销由 owner 事件、epoch 变更和本地控制通道承担。

## 信任边界与状态归属

### 权威组件

- 远端账户服务：账户 Session 是否被接受的权威来源。
- 本地 auth owner/bridge：当前账户身份、`runtime_instance_id`、`epoch` 和权威撤销事件的来源。
- Electron Main：机器与子进程生命周期、本地 capability token 的唯一发行方和轮换协调者。
- 本地 Python backend：token registry、HTTP/WS 边界校验和控制协议 ACK 的权威来源。
- Renderer：只消费脱敏状态和当前连接 descriptor，不拥有账户或 token 状态。

### 两层授权不能混为一层

账户授权回答“这个本地账户 scope 是否仍然存在”。本地 capability token 回答“这个 Renderer
请求是否来自当前 Electron、当前 backend 和当前 scope”。

scope token 的到期或注册失败不能证明账户退出；反过来，账户 scope 被撤销时，即使 bearer
尚未到期也必须立即拒绝。UI 只有在账户层进入 `LOCKED` 时才显示登录门。

## 账户状态语义

Desktop 本地连接使用以下状态，而不是一个全局 `authenticated` 布尔值：

| 状态 | 含义 | 本地能力 | 云端账户能力 | 登录门 |
| --- | --- | --- | --- | --- |
| `UNAUTHENTICATED` | 从未登录、无可信本地记录或已显式 logout | 拒绝 | 拒绝 | 显示 |
| `ACTIVE` | 最近一次权威校验成功 | 允许 | 允许 | 隐藏 |
| `CLOUD_UNREACHABLE` | 有可信本地 scope，但账户服务/bridge 暂时不可达 | 允许 | 降级并后台重试 | 隐藏 |
| `CLOUD_REAUTH_REQUIRED` | 云端 Session 需要重新认证，但没有权威本地撤销事件 | 允许 | 只在云端功能内要求重新认证 | 隐藏 |
| `LOCKED` | 显式 logout、权威账户 scope 撤销，或账户切换导致 epoch 变化 | 拒绝并清理 | 拒绝 | 显示 |

状态转换规则：

- `ACTIVE -> CLOUD_UNREACHABLE` 不改变 `runtime_instance_id` 或 epoch，不停止本地 backend。
- 重新连通并验证成功后回到 `ACTIVE`，用户界面不跳转。
- 仅有超时、DNS、TLS、429、5xx、无效响应或 bridge 进程临时不可用时，不得删除本地 scope。
- 显式 logout 立即进入 `LOCKED`，不等待远端 logout 请求完成。
- 账户服务返回可验证的账户 scope 撤销，或 auth owner 推送撤销/owner 换代时进入 `LOCKED`。
- 如果服务端只能说明云端 Session 需要重新认证、不能证明本地账户 scope 已撤销，则进入
  `CLOUD_REAUTH_REQUIRED`，不能借机把 token 轮换错误变成全局登出。

对于 App 重启：若 OS secure store 中存在完整、此前已在线验证且未被本地 logout tombstone
撤销的账户记录，Desktop 可以在账户服务不可达时恢复为 `CLOUD_UNREACHABLE` 并开放本地
能力；首次安装、记录缺失、记录损坏或 secure store 无法证明此前身份时仍为
`UNAUTHENTICATED`。这不会持久化 scope bearer：每次 auth owner 启动仍生成新的
`runtime_instance_id`，Electron 为该新 scope 发行全新的本地 token。

服务端不可达期间无法实时得知管理员刚执行的远端撤销，这是离线可用性的固有残余风险。
一旦恢复连通，校验立即优先执行；收到权威 scope 撤销后必须撤销本地 scope。此风险不能通过把
60 秒本地 bearer 当作远端租约来消除，因为短 bearer 只保护 loopback 调用，不提供新的
服务端事实。

## LocalCapabilityManager

Electron Main 新增窄模块 `LocalCapabilityManager`，避免继续把轮换状态扩散到 `main.ts`。
它按 backend/connection 保存：

- 当前精确 scope；
- backend 子进程和 base URL；
- 协议版本；
- `active` token 及其注册标识、签发时间和本地最早到期时间；
- 可选 `candidate` token；
- 每个 backend 唯一的 `refreshPromise`；
- 后台 timer、重试状态和生命周期 generation。

Manager 是本地 capability 的唯一写入者。其他 Electron 模块只能：

- 获取当前已确认的请求 descriptor；
- 通知 backend 启动、退出或 scope 变化；
- 请求一次本地恢复，但不能自行签发、注册或切换 token。

Renderer 仍只得到完成认证后的 descriptor；候选 token、注册标识、轮换进度和 ACK 不跨
preload bridge。

## Token 参数

| 参数 | 固定值 | 目的 |
| --- | --- | --- |
| bearer 熵 | 32 CSPRNG bytes / 256 bits | 防猜测和伪造 |
| token TTL | 1800 秒 | 降低正常轮换频率，并保留有限泄漏窗口 |
| 首次轮换时间 | 签发后 1200 秒 | 提供 10 分钟修复跑道 |
| 切换后重叠 | 最多 60 秒 | 吸收并发请求和 descriptor 传播延迟 |
| 正常并发轮换 | 每 backend 1 个 | 避免并发候选和乱序覆盖 |
| 控制帧上限 | 4096 bytes | 防止无界解析和日志注入 |

TTL 是 defense in depth，不再是账户在线租约。真正的撤销由 scope epoch 和显式 owner 事件
完成，因此把 TTL 从 60 秒提高到 30 分钟不会让 logout 后的 token 继续工作。

## 本地控制协议 v2

### 传输

- Electron 继续通过子进程受保护的 stdin 发送有界 NDJSON 控制帧。
- backend 通过现有 stdout 的保留控制前缀返回有界 ACK；stdout router 消费控制帧，普通日志
  继续进入现有日志通道。
- ACK 不包含 bearer、bearer digest、Cookie 或任何可重放 secret。
- 控制通道 EOF 仍清空该 backend 的全部 token registry。
- 控制消息使用严格 key set、精确版本和长度限制，未知操作或 schema drift 终止控制通道并
  触发本地 backend 恢复，不进入登录门。

### 标识与幂等

每次候选注册包含独立的 128-bit 随机 `registration_id`；每次提升包含独立的
`transition_id`。它们是关联标识，不是 bearer，也不具有授权能力。

backend 对相同 id 和相同内容幂等，对相同 id 的不同内容拒绝。Electron 只接受同时匹配
backend generation、精确 scope、registration/transition id 和当前 pending 操作的 ACK。
延迟、重复或乱序 ACK 不能改变活动 token。

### 两阶段轮换

#### 1. 注册候选

Electron 生成候选 token 并发送 `register_scope_token` v2 帧。backend 依次执行：

1. 严格解析 frame。
2. 校验 bearer 熵、connection id、scope 和 TTL。
3. 调用当前 `require_authorized()` 精确验证 scope。
4. 只在验证成功后把 bearer 的 SHA-256 digest 写入内存 registry，状态标记为 candidate。
5. flush `scope_token_registered` ACK。

在 ACK 到达前，Electron 不使用候选 token，也不修改活动 descriptor。

#### 2. 无副作用探测

ACK 成功后，Electron 用候选 bearer 调用专用本地 capability probe。该 endpoint 在 middleware
内完成，仅返回协议版本、匹配的 registration id 和精确 scope，不进入任何业务 handler，
不读写配置、Session、文件或模型状态。

probe 是第二重确认：ACK 证明控制线程已注册，probe 证明实际 HTTP 鉴权路径也接受同一候选。

#### 3. 提升与原子切换

probe 成功后 Electron 发送 `promote_scope_token`，包含候选 registration id、当前 active
registration id、transition id 和 60 秒 overlap。backend 在同一 registry lock 内：

1. 确认 active/candidate 都属于同一精确 scope 和 backend generation。
2. 把 candidate 标记为 active。
3. 把旧 active 标记为 overlap，并把其到期时间截短到最多 60 秒。
4. flush `scope_token_promoted` ACK。

Electron 只有在收到匹配 ACK，或 ACK 丢失后通过 candidate probe 明确读到相同 transition 已
完成，才把请求 descriptor 原子切到新 bearer。切换完成后，所有新请求使用新 bearer；已经
捕获旧 descriptor 的请求可在 overlap 窗口内完成。

如果注册、ACK、probe 或提升失败，Manager 保留旧 active token，丢弃或回收 candidate，按
带 jitter 的有界退避继续后台尝试。失败不能阻塞当前用户操作。10 分钟跑道用于容纳 backend
调度延迟、短暂重启和控制帧重试。

### 初次启动

初次启动没有旧 active token。backend 完成 candidate 注册后可直接执行无前任的 promote；
Electron 等待匹配 ACK，再用 capability probe 和正常 HTTP/WS 探针确认 backend 就绪。受保护
Renderer 仍然只在初始 token 和 backend 均已确认后挂载。

## 前台请求行为

正常轮换永远由 timer 发起，不得由模型配置保存、普通 fetch、WebSocket send 或 Renderer
导航发起。前台请求在开始时获取当前不可变 descriptor snapshot；轮换中的 candidate 对它
不可见。

若异常情况下请求仍收到本地 capability 拒绝，middleware 必须在进入业务 handler 前返回
结构化机器码，并明确 `failure_phase: pre_dispatch`。Electron 只在同时满足以下条件时自动
重试一次：

- 连接是 Desktop 本地 backend；
- code 是本地 capability 拒绝而非账户拒绝或 provider 401；
- backend 明确保证业务 handler 未执行；
- Manager 已经确认一个更新的 active descriptor；
- 原请求尚未因用户取消、connection/profile 切换或 generation 变化而过期。

这个一次性 retry 是异常恢复网，不是正常轮换路径。任何可能已经进入业务 handler 的 PUT、
POST、工具调用或模型请求不得盲目重放。模型配置保存的验收标准是业务写入恰好一次。

## WebSocket 语义

当前 WS ticket 不能继续把每条消息的有效性绑定到 30 分钟 bearer 的 registry 生命周期，
否则轮换仍会导致现有 socket 被关闭。

新语义如下：

- 建链时，ticket mint 和 upgrade 仍要求当前 active capability token。
- upgrade 成功后，WebSocket 绑定 `connection_id + runtime_instance_id + epoch + backend
  generation`，而不是绑定 bearer 的短期到期时间。
- 每条消息继续校验精确 scope 和 backend generation；不再复检已经轮换掉的 bearer digest。
- 正常 token 轮换不关闭、不重建 WebSocket。
- logout、权威撤销、owner 换代、connection/profile 切换、backend 退出或控制通道 EOF 必须
  主动关闭相应 socket，并拒绝旧 ticket。

这样 bearer 仍保护建链入口，而 scope epoch 负责长期连接的撤销。

## 错误分类与 UI

backend 不再把所有 `AuthRequired` 统一映射为 `Ansatz login required`。至少区分：

| 分类 | 示例机器码 | UI/恢复行为 |
| --- | --- | --- |
| 本地 capability | `local_capability_rejected` | Main 内部轮换/恢复；不显示登录 |
| 本地 backend | `local_backend_unavailable` | 本地重连、重启或 runtime 修复；不显示登录 |
| 账户服务连接 | `cloud_auth_unreachable` | 保留本地 UI，云端区域显示降级 |
| 云端重新认证 | `cloud_reauth_required` | 只限制依赖云端 Session 的功能 |
| 账户 scope 锁定 | `account_locked` | 清理本地能力并显示登录门 |
| 第三方 provider | 现有 provider code/HTTP 状态 | 模型配置或请求上下文内显示 |

`Ansatz login required` 文案只允许由 `account_locked`/真正未登录边界产生。正常轮换、候选
token、控制 ACK、probe、local backend 恢复和 provider 401 均不得产生该文案。

Renderer 不展示轮换进度。只有恢复耗尽并导致整个本地 backend 暂时不可用时，才使用现有
“本地服务恢复/修复”体验；它仍不能跳转登录页或清除账户身份。

## 显式撤销与隔离

以下事件绕过 TTL 和 overlap，立即执行：

1. Electron 先提升本地 lifecycle generation，停止分发新 descriptor。
2. auth owner 提升 epoch 或发布 locked 状态。
3. `LocalCapabilityManager` 清除 active/candidate、取消 timer 和 pending promise。
4. Electron 关闭控制 stdin；backend 清空 registry，并关闭该 scope 的 WebSocket。
5. 在途操作在下一个授权边界停止；旧结果因 generation/scope 不匹配不能发布。
6. 显式 logout 最后再尽力通知远端服务；远端不可达不能撤销已经完成的本地锁定。

账户切换必须生成新的 runtime instance/epoch，不能复用旧 token、registration id、WS ticket、
descriptor、请求 retry 或 profile cache。

## 版本与发布策略

backend 的 ready/control 输出声明 `desktop_scope_protocol: 2`。Electron 只有在确认 v2 后才创建
`LocalCapabilityManager` 的最终轮换路径。

由于 Desktop 与本地 runtime 可能更新不同步：

- 安装包同时携带支持 v2 的 Electron 和 Python runtime。
- 发现旧本地 runtime 时，先走现有受验证的 runtime 更新/修复梯子；更新完成后再开放本地
  backend。
- 更新或修复失败呈现为本地 runtime 故障，不是账户登录失败。
- 不把 30 分钟 TTL 静默发给只支持 v1 的 backend，也不保留会触发前台竞态的 v1 轮换作为
  长期兼容路径。
- remote/cloud connection 继续使用各自已有协议，不参与本地 v2 协商。

本次不提交紧急热修。实现、fault-injection 测试、捆绑 runtime 和 Desktop 必须作为一个完整
版本发布。

## 可观测性与秘密处理

只增加本地结构化诊断事件，不新增 outbound telemetry：

- `scope_rotation_started`
- `scope_candidate_acknowledged`
- `scope_candidate_probe_succeeded`
- `scope_rotation_promoted`
- `scope_rotation_retry_scheduled`
- `scope_rotation_recovered_backend`
- `scope_revoked`

事件可包含连接类别、backend generation、阶段、耗时、重试次数和安全 reason code；不得包含
bearer、digest、Cookie、CSRF、完整 registration/transition id、用户名或请求 body。普通运行
不记录每次请求成功，避免日志噪声。

现有日志中的 `Ansatz login required` 计数应成为验收信号：正常轮换压力测试中必须为零。

## 测试设计

### TypeScript 单元测试

- 30 分钟 TTL、20 分钟轮换点、60 秒 overlap 和 256-bit entropy。
- 每 backend single-flight；100 个并发 timer/recovery 信号只触发一次轮换。
- 延迟、丢失、重复、伪造和乱序 registration/promotion ACK。
- scope、backend generation 或 transition 不匹配的 ACK 被忽略。
- 注册/探测/提升任一步失败时旧 active 不被提前替换。
- timer、重试和 logout/profile switch 的竞争使用 fake clock 验证。
- token、registration id 和控制状态不跨 preload bridge。

### Python 单元测试

- v2 严格 schema、帧上限、bearer entropy 和精确 scope 校验。
- registry 的 candidate/active/overlap 状态机和幂等 transition。
- promotion 在同一 lock 内完成；旧 token 最多再用 60 秒。
- logout、epoch 变化、control EOF 和 backend shutdown 立即清空全部 grant。
- capability probe 无业务副作用，candidate 只能访问 probe。
- HTTP 中间件区分 local capability、account lock 和业务/provider 错误。
- WS 建链受 active token 保护，建链后只绑定 scope/generation，轮换不关闭 socket。

Python 测试通过 `scripts/run_tests.sh` 运行；Desktop 测试通过仓库现有 Vitest/打包测试命令
运行。测试调用真实模块行为，不能读取源码文本或只断言常量存在。

### 跨进程集成测试

启动真实 Electron-side child harness 和 Python backend，使用临时 `HERMES_HOME`：

1. 在连续 REST GET、配置 PUT、WebSocket 消息和模拟模型请求期间跨过至少三次轮换。
2. 给 backend 控制线程注入 50ms、500ms 和 5s 延迟，所有前台请求仍成功。
3. 丢弃首次 ACK、重复 ACK、逆序发送旧 ACK，最终只提升最新候选。
4. 在 promotion 前后杀死并恢复 backend，错误保持为本地恢复，不出现登录门。
5. 并发保存模型配置时，业务 handler 和磁盘写入都恰好执行一次。
6. Trace 上传和第三方模型请求在轮换期间不被取消或重放。
7. 保持账户服务不可达跨过多次轮换，本地能力和现有 WS 持续可用。
8. 恢复账户服务并返回权威 scope 撤销，scope、token、WS 和本地 backend 立即锁定。
9. 显式 logout 在 overlap 窗口内发生时，新旧 token 均立即失败。
10. 切换 connection/profile 后，旧 token、旧 ACK、旧 retry 和旧 WS 均不能影响新 scope。

### 打包验收

- macOS DMG 和 Windows NSIS 的捆绑 runtime 均声明并实际支持协议 v2。
- clean install 首次登录、App 重启、backend 重启和 runtime repair 路径行为一致。
- 账户服务暂时不可达时，已有可信账户记录可以恢复本地降级态；全新安装仍要求登录。
- 日志、crash report、renderer diagnostics、argv、env 和磁盘均无 bearer。
- 正常轮换日志中 `Ansatz login required` 为零。

## 验收标准

实现只有同时满足以下条件才算完成：

1. 在账户未显式 logout、未收到权威 scope 撤销且 scope 未变化时，token 生命周期不会改变用户操作、
   UI、登录状态或请求结果。
2. 正常轮换不进入任何前台请求依赖链，且不会中断 HTTP、WS、模型配置、模型请求或 Trace。
3. 任何本地 token/backend 错误都不能映射为登录错误。
4. 服务端不可达时本地继续可用；恢复连通后的权威撤销仍立即生效。
5. 旧 token 在正常提升后最多重叠 60 秒，在 logout/epoch change 时零宽限失效。
6. provider 401 只显示为 provider 错误。
7. 配置等非幂等写入不因恢复逻辑重复执行。
8. token 和账户 secret 不进入 Renderer、URL、env、持久化或日志。
9. v1/v2 不匹配被识别为本地 runtime 兼容问题，不产生 `Ansatz login required`。
10. 所有单元、跨进程、故障注入和打包验收通过。

## 预期实施边界

后续实施计划预计涉及：

- Electron：`auth-scope-token.ts`、新 `local-capability-manager.ts`、backend stdout/control router、
  `main.ts` 的窄接线、auth coordinator 的降级态和对应测试。
- Python：`hermes_cli/client_auth/runtime.py` 的 registry/control v2、
  `hermes_cli/web_server.py` 与 `gateway/platforms/api_server.py` 的错误分类和 WS 边界，以及测试。
- Renderer：只调整真正账户锁定、本地恢复和云端降级的状态映射/文案；不实现 token 轮换逻辑。
- 打包：确保 Electron 与捆绑 runtime 的 v2 能力成对发布。

具体文件名可以在不改变上述权威边界、协议和验收条件的前提下由实施计划细化。
