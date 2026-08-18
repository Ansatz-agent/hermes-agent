# Hermes 远程账户硬门禁设计

- 日期：2026-08-18
- 分支：`feature/remote-auth-hard-gate`
- 基线：本地 `main`（`4ef56ce`）
- 固定认证服务：`https://c2sml.cn/agent`
- 状态：产品设计已确认，第四轮审查意见已纳入，等待最终独立审查

## 1. 目标

在未经修改的官方 Hermes 构建中，用户必须先通过远程 Django 账户认证，才能使用任何能够启动 Agent、读取对话、调用工具、访问文件或终端、执行后台任务、连接能力后端或产生外部副作用的功能。

门禁必须覆盖：

- Electron Desktop
- 经典 CLI
- Ink TUI
- Dashboard 与 ACP
- `gateway`、`serve`、cron、MCP、worker、delegate、kanban
- 本地 backend、SSH remote backend 与 headless Docker
- 插件、脚本入口、安装服务和容器启动路径

认证与远程记忆框架解耦。记忆服务器的数据模型、检索方式或对话存储框架发生变化时，只要 Django Session 契约不变，客户端登录模块不迁移。

## 2. 非目标与威胁边界

本门禁是官方产品行为，不是针对机器所有者的 DRM。能够替换二进制、回退到旧提交、注入进程，或以 root/同一 OS 身份的任意本机代码使用调试与进程读取权限的主体，不在客户端的强秘密隔离模型内。Broker 仍必须做进程加固，降低普通子进程、崩溃报告和误配置造成的 Session 泄漏；但规格不声称在同一用户已经执行恶意原生代码后仍能提供硬隔离。

硬保证分为两层：

1. 未经修改的官方客户端在没有有效认证租约时不开放任何 Hermes 能力。
2. 服务端记忆 API 和未来的远程推理 API 独立校验 Django Session；没有有效 Session 的请求不能取得服务端数据或计算能力。主机已被攻陷后窃取有效 Session 属凭据失窃，不是服务端鉴权绕过。

第一阶段不实现：

- 客户端注册、找回密码或修改密码
- 多账户切换
- 自定义认证服务器
- 关闭 TLS 校验
- 离线模式或认证失败后的额外 grace
- 把本机 Session 转发给远程主机
- SSH/headless 登录跨主机、Broker 或容器重启自动恢复
- 长期设备令牌、service account 或 secret-provider 扩展框架

SSH 与 headless Docker 是支持范围，不因缺少图形系统而禁用；它们采用只存在于内存的临时登录，重启后重新登录。

## 3. 已确认的产品约束

1. 账户只能由服务器管理员通过 Django 管理端或服务器运维流程创建、停用和重置。Desktop、CLI、TUI、Broker 与公开客户端 API 都不提供注册、创建账户、自助找回或修改密码入口。
2. 本地图形环境保持登录；密码永不持久化，只保存 Django Session/CSRF Cookie。
3. 本地图形环境只使用 macOS Keychain、Windows Credential Manager 或 Linux Secret Service 保存 Cookie，不提供普通文件回退。
4. SSH/headless/Docker 的 Cookie 只存在远端认证 Broker 内存中，不写磁盘、配置、环境变量或 Docker layer。
5. 启动和运行时均需认证。网络、TLS、5xx、429、Session 失效或 Broker 故障使运行时锁定。
6. 固定认证服务器为 `https://c2sml.cn/agent`，客户端没有 server URL 或 insecure 开关。
7. 同一 OS 用户的 Hermes profiles 共用一个远程账户；`logout` 锁定该用户的所有 profiles。
8. SSH/headless/Docker 重启后允许要求用户重新登录，不要求无人值守恢复认证。

## 4. 精确的未登录白名单

原始 argv 只允许以下形状：

- `hermes login`
- `hermes logout`
- `hermes auth status`
- `hermes --help` / `hermes -h`
- `hermes --version` / `hermes -V`

任何额外 token、全局 flag、`--`、重复 flag、合并短 flag、未知命令或插件命令都受保护。未登录帮助是静态内置文本，不加载插件、Agent、工具、会话或动态 parser 扩展。

现有 provider 认证命令迁移到受保护的 `hermes provider ...`。精确裸 `login`、`logout` 和 `auth status` 归远程 Hermes 账户。`hermes logout` 明确输出“远程 Hermes 账户已退出，provider 凭据未修改”，避免旧脚本静默改变含义。

`doctor`、`update`、`logs`、`uninstall`、服务安装和容器管理不在白名单中。认证运行时损坏时，只显示不调用 Hermes 能力的静态修复说明。

## 5. Django 最小契约

保留现有 Django LoginView、LogoutView 和 Cookie 名：

- `agent_history_sessionid`
- `agent_history_csrftoken`

新增机器可读验证端点：

```http
GET /agent/api/session/
```

响应契约：

- 已登录：`200 application/json`，严格 schema 为 `{"authenticated":true,"username":"...","server_time":"...","session_expires_at":"..."}`。
- 未登录：`401 application/json`，严格 schema 为 `{"authenticated":false}`。
- 不返回 HTML、重定向或 Cookie 值。

该只读端点不计入 django-axes 的登录失败计数；LoginView 继续保留失败计数与限流。客户端只接受固定 HTTPS origin、JSON Content-Type 和预期 schema。HTML、captive portal、跨域或降级重定向均归为认证服务不可用。

Django 必须为每个账户 Session 记录不可滑动的绝对过期时间，并通过 `session_expires_at` 返回。Session 验证与 60 秒轮询不得延长该时间；到期后必须重新输入密码。具体期限由固定服务端策略管理，客户端和 Broker 不能覆盖。

登录流程：

1. GET LoginView 获取仅存在于当前请求内存中的 CSRF。
2. POST 用户名和密码。
3. 调用 Session JSON 端点进行二次验证。
4. 成功后将 Cookie 交给当前运行时的唯一 secret owner。

每次合法响应只接受带 `Secure` 且 Path 与 `/agent/` 匹配的白名单 Cookie。Session 与 CSRF Cookie 作为一条版本化记录原子更新，避免两个 Cookie 在进程中断时失配。

Logout 顺序固定为：先递增 epoch 并锁定本地运行时，再尽力 POST 远端 LogoutView，最后总是清除本地或内存 Session。

客户端认证契约不包含 signup、account-create、password-reset 或 invitation endpoint。Django 管理端可以保留管理员专用账户生命周期能力，但不能通过 Hermes 客户端 bridge、Broker IPC 或公开用户路由暴露。

## 6. 最小客户端架构

认证实现是一个 Python 包和四个职责模块：

```text
hermes_cli/client_auth/
  guard.py      stdlib-only；精确 argv 白名单和入口预检
  runtime.py    Broker、leader、存储模式、lease、epoch 与边界校验
  client.py     固定 Django HTTP、CSRF、Session 和错误分类
  bridge.py     仅供 Electron Desktop 使用的版本化 JSONL 接口
```

另有一个声明式 `entrypoints.json`，只记录入口标识与 `interactive` / `noninteractive` 策略。CI scanner 发现真实入口集合并要求与 manifest 完全相等。

不创建独立的 AuthGuard、AuthMonitor、AuthBoundary、EntrypointRegistry、Store 或 Docker Supervisor 状态对象。`runtime.py` 是唯一认证真值；所有执行位置调用同一个原语：

```text
require_authorized(boundary)
```

`guard.py` 保持 stdlib-only，必须能在完整 CLI parser、插件与 Agent 模块导入前运行。安全边界复检在 `runtime.py`，避免把完整运行时依赖带入启动预检。

`runtime.py` 内部只有一套租约协议和两种私有 secret owner：`VaultOwner` 与 `MemoryOwner`。两者发布同样的状态记录、使用同样的活性连接、epoch 和边界校验；区别只在 Cookie 存放位置。不能分别实现“leader 状态机”和“Broker 状态机”。

Ink TUI 复用现有 `tui_gateway` JSON-RPC 增加登录方法，不再启动第二个 Python auth bridge。Electron 仍需要 `bridge.py`，因为它在 Hermes backend 启动前必须完成认证。

## 7. 两种 Session 所有权模式

### 7.1 本地图形环境：OS vault

适用于已解锁的 macOS、Windows 或带 Secret Service 的 Linux 桌面会话。

- 当前 OS 用户的 runtime owner 是 vault 唯一写者，并通过与 headless 相同的本地 IPC 发布租约。
- Cookie 作为一条版本化 blob 原子保存。
- 密码只存在登录调用内存中，验证结束后立即释放引用。
- Desktop 或 CLI 重启时从 vault 读取 Cookie 并在线验证，因此支持保持登录。
- vault 锁定、不可用或记录损坏时 fail closed。

### 7.2 SSH/headless/Docker：内存 Broker

远端 `hermes-auth-runtime` 是该 OS 用户或容器中的唯一 Session owner：

- Cookie 只存在 Broker 进程内存。
- 其他进程通过受保护的本地 IPC 获取无秘密的状态、lease、`runtime_instance_id` 与 epoch，并在使用期间保持一条 owner 活性连接。
- Broker 是远端唯一访问 Django Session 端点和轮换 Cookie 的进程。
- Broker 退出、主机重启或容器重启后登录状态消失。
- 远端进程永远不能读取 Cookie、CSRF 或密码。

SSH 主机上的交互式登录会启动一个与当前 SSH 控制终端和标准文件描述符脱离的用户级 Broker；父进程必须等到受保护 IPC 就绪后才报告登录成功。Broker 不继承登录 stdin。所有非必要 fd 在 spawn 前设为 `CLOEXEC`，登录结束后对可控密码 `bytearray` 做最佳努力覆写并立即释放引用；规格不虚假承诺 Python 与 SSH 内部产生的全部副本都可被可靠清零。若主机安全策略在最后一个 SSH 会话结束时杀死全部用户进程，则 Broker 同时消失并使 Hermes 锁定；客户端不通过自动执行 `loginctl enable-linger`、降低主机策略或持久化 Session 绕过该行为。

跨平台 owner 传输定义如下：

- Linux 与 macOS 使用用户专属 Unix Socket。目录权限为 `0700`，Socket 为 `0600`；Linux 校验 `SO_PEERCRED`，macOS 校验 `getpeereid()`。
- Windows 使用名称由当前用户 SID 派生的 Named Pipe；Pipe DACL 只允许该 SID 与 SYSTEM，服务端 impersonate 客户端并校验 token SID/进程身份。
- Docker 使用容器内 `/run` 的临时 runtime 目录并以非 root Hermes 用户运行。

runtime 路径由 OS 用户身份派生、独立于 `HERMES_HOME` 和 profile，不接受 CLI 或环境变量 override。Windows OpenSSH、Windows 计划任务、macOS SSH/LaunchAgent 与 Linux SSH 使用同一租约协议和对应原生传输；没有原生传输时 fail closed，而不是使用普通文件或 TCP 回退。

Broker IPC 动词精确限制为 `{status, login, logout}`：

- `status` 只返回无秘密状态。
- `login` 只接受一次登录凭据并受本地尝试次数/时间窗限流；不得充当任意 Django 代理。
- 已认证时再次执行 `hermes login` 只输出当前状态，不提示密码、不递增 epoch。切换账户必须先 logout。
- `logout` 只锁定当前执行主机的 OS-user runtime。
- 禁止返回 Cookie/CSRF、代理 HTTP、执行命令或扩展任意 RPC 动词。

同 UID 恶意进程仍可能调用 logout 造成拒绝服务；这是明确接受的残余风险。登录端本地限流与 django-axes 共同限制同 UID 进程利用 Broker 锁号。

Broker 启动时必须禁用 core dump 和可附加调试：Linux 使用 `PR_SET_DUMPABLE=0` 与 `RLIMIT_CORE=0`，macOS 使用 `PT_DENY_ATTACH`/`RLIMIT_CORE=0`，Windows 禁用该进程的 WER dump 并应用可用的进程缓解与最小 DACL。任一必需加固失败时 MemoryOwner 拒绝进入 authenticated。Cookie 内存尝试 `mlock`/平台等价锁页；锁页失败但 core/ptrace 加固成功时允许运行，并把“加密 swap 或休眠镜像可能包含内存页”列为已接受残余风险。Broker 崩溃报告不得包含 heap、请求头或 dump。

Broker 的硬过期时间不得超过服务端 `session_expires_at`。没有任何消费者活性连接且没有受保护任务时，15 分钟后空闲退出；周期验证本身不算活性。持续运行的 gateway/serve/cron 可保持活性连接，但仍不能超过服务端绝对 Session 期限。

Socket/Pipe 名和无秘密状态可以存在于 runtime 目录，Session 不写入该目录。

## 8. OS-user 运行时、租约与撤销

在线验证单位是当前 OS 用户的 Hermes 认证运行时，不是每个 Python 或 Node 子进程。

- 第一个受保护入口取得 OS 咨询锁并启动或连接唯一 runtime owner，由 owner 读取 secret 并在线验证。
- 同时启动的入口合并为一次远端请求。
- worker、MCP、delegate、kanban、TUI backend 和 Desktop backend 只读取当前 lease 与 epoch，不各自请求 Django。
- 长期进程由唯一 runtime owner 周期验证。owner 死亡后仅入口门禁可以重新选举；正在执行的消费者因活性连接断开立即锁定，不在工具调用中临时联网。
- vault 与 Cookie rotation 始终只有一个写者。

授权状态只有四个：

- `checking`
- `authenticated`
- `signed_out`
- `locked`

错误差异使用 reason code，不复制状态机：

- `invalid_credentials`
- `session_expired`
- `server_unavailable`
- `rate_limited`
- `vault_unavailable`
- `runtime_unavailable`

权威运行字段只有：

- `state`
- `epoch`
- `valid_until`
- `boot_id`
- `runtime_instance_id`

`runtime_instance_id` 是每次 owner 启动时生成的 128-bit CSPRNG nonce。`username`、`checked_at` 和 `leader_pid` 只用于展示或诊断，不参与授权判定。

`valid_until` 使用包含系统休眠时间的 boot-relative 单调时钟；boot ID 变化、时钟不可比较或 deadline 到期均视为过期。状态缺失、不可读、owner/mode 错误、schema 不符、活性连接断开，或 `(runtime_instance_id, epoch)` 不等，一律 `locked`。

验证周期固定为 60 秒并带小抖动。网络重试只能发生在当前 `valid_until` 之前，不能延长 deadline。周期检查发现失败后立即递增 epoch 并锁定，不增加额外 grace。60 秒是显式撤销检测窗口，不是离线授权。

每个消费者保持一条 runtime 活性连接，由 owner 推送状态/epoch 变化。`require_authorized()` 在热路径读取进程内的最新原子状态，不为每条 HTTP/WS 消息做 IPC 往返；活性连接断开必须绕过任何缓存立即锁定，租约到期则提供第二层 fail closed。

## 9. 安全边界与锁定传播

所有官方进程在以下位置调用 `require_authorized(boundary)`：

- Agent turn 和外部请求开始
- session、history 或 memory 读取
- delegate、kanban 或 worker 子任务开始
- 每次工具调用
- terminal 命令和后台进程启动
- 文件写入、移动或删除
- git push、PR 或其他远端操作
- 消息发送
- 计费或网络副作用
- gateway、serve 的每个 HTTP 请求和 WebSocket 消息
- cron tick 与 job 开始
- MCP 和 ACP 请求

授权必须对 `(runtime_instance_id, epoch)` 做相等比较，不能只比较 epoch，也不能使用大于等于。所有 HTTP/WS token、worker 授权和边界上下文都携带该二元组。锁定、owner 换代或重新登录后，旧 worker、旧连接和旧 token 永远不能复活。

在途任务在下一个边界停止，不再产生新的外部副作用。已经完成且不可逆的外部操作不回滚。

## 10. 入口覆盖与启动顺序

`entrypoints.json` 是声明集合和交互策略；scanner 是发现集合。两者职责不同且必须相等。scanner 是 CI 安全分析工具；它自己的测试在临时 fixture tree 上执行 scanner 行为，不读取生产源码后用正则断言实现形状。生产入口仍需通过真实未认证启动测试证明 fail closed。

scanner 覆盖：

- `pyproject.toml` 的所有 scripts
- Python `__main__.py` 和带 main guard 的顶层模块
- `.py`、`.ts`、`.tsx`、`.js`、`.mjs`、`.sh` 中的 spawn、subprocess、exec 和 `python -m`
- Electron 与 Ink 启动的后端
- Docker entrypoint、s6、systemd、launchd、Windows 服务或计划任务安装脚本

人工入口列表不进入规范正文；已发现入口作为测试 fixture。manifest 只保存 scanner 无法推导的 `interactive` / `noninteractive` 策略。

CLI 模块导入期的精确顺序固定为：

1. 只允许 `hermes_bootstrap`、Windows stdio/subprocess 兼容补丁和 stdlib-only `_startup_fast`。
2. 精确 `--version/-V` 与静态 `--help/-h` 快路径可以直接返回。
3. stdlib-only `client_auth.guard` 对其余 raw argv 执行共享门禁。
4. 只有门禁通过后，才能运行 `_early_recovery.recover_if_needed()`、`_apply_profile_override()`、dotenv/config 加载和完整 parser import。

认证 runtime 与 profile 无关，因此把 `_apply_profile_override()` 放在门禁之后不会改变账户身份。门禁还必须发生在以下行为之前：

- Agent、tools、plugins、MCP 或 session 模块导入
- `_cleanup_quarantined_exes()`、`_sweep_stale_bytecode_if_checkout_changed()`、`_recover_from_interrupted_install()` 等可能产生副作用或 spawn 的自愈逻辑
- 容器 exec 转发
- backend、worker 或 helper 进程 spawn

`_cleanup_quarantined_exes()`、`_sweep_stale_bytecode_if_checkout_changed()` 与 `_recover_from_interrupted_install()` 位于 `main()` 内，也必须在共享门禁之后。其他自愈、update 或 repair 不获得隐式豁免。

## 11. Desktop

### 11.1 本地启动

1. Electron 只启动登录壳和签名 runtime bootstrap。
2. 全新安装仅开放：`hermes:bootstrap:get`、`continue-local`、`repair`、`reset`、`cancel`。
3. Python auth bridge 可用后验证本地 vault Session。
4. 认证成功后才运行 `runPrimaryBackendStartup`、启动 `hermes serve`、注册能力热键或 deep link，并显示主界面、HUD、Quick Entry、Pet Overlay 和 terminal。
5. 锁定时先递增 epoch，再撤销入口、关闭能力窗口和后端连接。

### 11.2 IPC 与 backend 直连

Electron 使用统一 `guardedIpc` 默认拒绝。唯一适配层之外不直接注册 `ipcMain.handle/on`。`AUTH_FREE_CHANNELS` 只包含 auth、窗口 close/minimize、主题、脱敏错误报告和前述 bootstrap channel，并以注册表行为契约测试覆盖；测试不读取 `main.ts` 源码，也不依赖 handler 数量。

IPC 只是第一层。Renderer 获得的所有 backend HTTP/WS token 都绑定当前 auth scope 的 `(runtime_instance_id, epoch)` 和短 TTL：

- backend 在 HTTP 请求、WS 建链及每条 WS 消息校验完整 auth scope。
- 锁定时 backend 关闭连接并作废旧 token。
- 旧 token 直接连接 HTTP 或 WS 必须失败。
- Renderer 永远拿不到 Django Cookie、CSRF 或 vault 内容。

### 11.3 SSH remote backend 登录

1. Desktop 建立普通 SSH 连接，只启动受限的远程 auth bridge，不启动 Hermes backend。
2. auth bridge 查询当前 OS 用户的内存 Broker。
3. 未登录时 Desktop 在本地显示账号密码页。
4. 凭据经加密 SSH 连接的 stdin 发送给远端 bridge；不进入 argv、env、文件或日志。
5. 远端 Broker 直接向固定 Django 服务登录并持有 Cookie。
6. Broker 只返回 `authenticated`、`username`、`runtime_instance_id`、`epoch`、`valid_until` 和 `session_expires_at`。
7. 登录成功后才启动 remote backend，并通过 SSH tunnel 返回绑定 auth scope 的短期 HTTP/WS token。

Desktop 不把本机 Keychain Session 转发给远端，也不缓存远端 Session。Remote connection 切换时，认证状态以对应远端 Broker 为准。

承载账户密码的 SSH 连接必须严格校验 host key：

- 已知 key 必须精确匹配；变化时 hard fail。
- 未知主机必须在 Desktop UI 显示 fingerprint，并由用户显式确认一次 TOFU；不得静默 `accept-new`。
- 该凭据路径禁止 `StrictHostKeyChecking=no`、自动接受未知主机或临时空 `UserKnownHostsFile`。
- 实施 SSH 阶段前必须专项审计 `ssh-config.ts`、`ssh-connection.ts` 与 `ssh-bootstrap-coordinator.ts`；现有 `accept-new` 只能在改成显式确认后承载账户密码。

### 11.4 多连接认证作用域

认证按执行目标/connection scope 管理，不是一个全局布尔值：

- local connection 使用本机 runtime owner。
- 每个 SSH connection 使用对应远端 owner。
- 每个运行中的 backend、窗口请求和后台任务都携带 `connection_id + runtime_instance_id + epoch`。
- 当前连接未认证时，只显示该连接的登录壳；不会因为另一连接已认证而放行。
- 一条连接锁定或 logout 不连带锁死其他仍有效的 local/remote connection。
- Desktop 切换 connection 时先撤销旧连接在前台窗口的 IPC/HTTP/WS 能力，再验证新连接 scope；后台连接只在自己的 scope 内继续运行。
- Electron 本机特权 IPC 必须使用发起窗口/会话绑定的 connection scope，不能引用“当前全局连接”造成跨连接授权。
- 应用首次启动仍是硬门禁：用户必须选择并认证一个目标 connection 后才能进入对应工作区。

## 12. CLI、TUI、ACP 与后台入口

- 经典 CLI：缺失或失效时在终端读取用户名与 `getpass` 密码。
- Ink TUI：Ink 显示登录表单，使用现有 `tui_gateway` RPC 将凭据经 stdin 交给 Python；TUI backend 本身保持 noninteractive。
- ACP/编辑器：返回协议级 `AUTH_REQUIRED` 和“请先运行 `hermes login`”提示，不弹密码输入。
- gateway、serve、cron、MCP、worker、计划任务：noninteractive；认证无效时进入 `locked-waiting` 或返回拒绝，不读取密码。
- 所有认证拒绝使用进程退出码 `20`；具体原因通过结构化 stderr、RPC 或协议错误返回。

纯 SSH 用户运行 `hermes login`；任意交互式受保护命令也可以引导登录。在允许用户进程脱离 SSH 会话继续运行的主机上，Broker 可继续为同一 OS 用户的 gateway、serve 或 cron 提供认证，直到 Broker、主机或 Session 失效；不允许时会安全锁定并要求下次重新登录。

服务安装命令本身受登录门禁。已安装服务在主机启动后只允许启动对应平台的 runtime owner，并进入 `locked-waiting`；没有新的交互登录时不能启动 Agent 或能力 backend。`locked-waiting` 是正常稳态：使用结构化状态与脱敏日志，不算启动失败、不触发 systemd/launchd/Windows/s6 重启风暴，也不计入 gateway restart-loop guard。cron 到期但未登录时记录脱敏的 `AUTH_REQUIRED`，不执行 job，也不无限重试。

`hermes login` 在有效 owner 上是幂等 status；仅在 signed_out/locked 且为交互入口时采集凭据。`rate_limited` 且 `Retry-After` 超过剩余租约时会按零宽限策略锁定，UI 必须明确显示“认证已锁定，请在限流结束后重新登录”，不能表现为普通连接错误。

## 13. Headless Docker

Hermes 容器复用现有 tini/s6 生命周期，不增加独立 Docker supervisor。未登录启动面只有作为 s6 服务运行的 auth runtime：

- 容器启动时 Agent、gateway、serve、cron runner 和能力 backend 的 spawn 数为零。
- 用户运行 `docker exec -it <container> hermes login`，或由 Desktop 经 SSH 完成远端登录。
- `container_boot.py` 在每次启动时保留各 profile 的期望运行意图，但把 gateway、dashboard、serve、cron 和 main Hermes 服务全部注册为 down，不再因先前状态为 `running` 而自动拉起。
- 登录成功后 auth runtime 的 s6 生命周期适配器按保存的运行意图把现有服务置为 up；锁定时把能力服务置为 down。
- Session 只存在 Broker 内存，不写 volume、image layer、env 或 Docker secret。
- 容器重启后恢复锁定并要求重新登录。
- Broker 或认证失效时 s6 保持 auth runtime，停止能力服务，并进入 `locked-waiting` 等待下一次交互登录。

容器入口不能先 exec 进入普通 Hermes CLI 再在容器内自行决定是否认证；auth runtime 与现有 s6 服务依赖/控制关系是默认拒绝边界。`container_boot.py`、`entrypoint-dispatch.sh` 和 `stage2-hook.sh` 必须通过真实容器测试证明不会在未登录时恢复能力服务。

## 14. 错误与用户体验

Desktop 登录页只包含固定服务说明、用户名、密码、登录、重试和错误；不显示注册、服务器地址、离线进入或跳过。

错误显示规则：

- 错误原因使用人类可读文案，不展示服务端 HTML、Cookie、请求头或堆栈。
- `invalid_credentials` 不自动重试。
- `rate_limited` 遵守 `Retry-After`，但不能延长当前租约。
- 首次登录遇到网络错误时保持未登录，只提供重试。
- 运行中验证遇到网络、TLS、5xx、429 或非法响应时 fail closed。
- Broker 不存在时，交互式入口可启动或连接 Broker；非交互入口返回 `AUTH_REQUIRED`。
- Broker 运行中突然消失时，所有消费者立即视为 `locked`。
- MemoryOwner 绝对到期或 15 分钟无消费者空闲退出时显示“登录已结束，请重新登录”，不伪装成网络错误。

文案同步 `en`、`ja`、`zh`、`zh-hant`；其他 locale 明确 fallback 到 `en` 并测试。

## 15. 测试设计

实现必须失败测试先行，至少覆盖以下不变量：

1. 精确 argv 白名单、所有形变，以及 guard 早于 `_early_recovery`、profile override、自愈、容器转发和 backend spawn。
2. scanner 发现集合与 manifest 声明集合完全相等；已发现入口保留为 fixture。
3. 未认证执行受保护入口时，无能力模块 import、session 读取、额外网络 socket、Popen 或 exec。
4. 同一 runtime 协议在 VaultOwner/MemoryOwner 下行为一致：并发合并、单写、咨询锁、活性连接断开、owner 死亡和换代。
5. `runtime_instance_id + epoch` 向 worker、MCP、gateway、HTTP/WS token 和工具边界传播；owner 换代后旧 token 必拒绝。
6. lease 过期、休眠、墙钟跳变、boot ID 变化、服务端绝对 Session 到期和状态损坏均锁定且不续期。
7. vault 版本化 blob 原子写、Cookie rotation 与本地保持登录。
8. MemoryOwner 的 core/ptrace/dump/fd 加固、无 Cookie 持久化、空闲退出、绝对到期和 crash report 脱敏。
9. Linux/macOS Socket 与 Windows Named Pipe 的 owner/mode、peer identity、动词白名单、本地限流和伪造客户端拒绝；同 UID logout DoS 作为显式残余风险。
10. Desktop IPC 默认拒绝、`AUTH_FREE_CHANNELS` 行为契约、全新安装登录，以及认证前 backend spawn/connect 为零。
11. HTTP/WS token 绑定完整 auth scope、锁定断链和旧 token 拒绝。
12. Desktop local + 多 remote connection scope 隔离、切换/rehome、前台与后台路由，以及一条 connection logout 不影响其他 connection。
13. Ink TUI 登录、ACP 错误、noninteractive 拒绝与服务 `locked-waiting` 不触发重启循环。
14. SSH 严格 host key、显式 TOFU、凭据仅走 stdin、fd 不继承，且 argv/env/磁盘/日志无密码。
15. Docker s6 auth runtime、`container_boot.py` 全部能力槽注册 down、登录后按意图启动、失效 down 和重启清空 Session。
16. Django CSRF/login/session JSON/Cookie rotation/logout/axes、不可滑动 `session_expires_at` 和验证不续期。
17. 服务端 memory API 在无 Session 时拒绝。
18. profiles 共用 OS-user runtime、跨平台原生 runner 测试和所有入口的真实未认证启动测试。

Python 测试通过 `scripts/run_tests.sh` 运行。Desktop/TUI 使用真实模块行为测试，不用读取源码文本或只比较硬编码数量的 change-detector 测试。跨 OS 行为在对应原生 CI runner 上验证，不伪造 `sys.platform`。

## 16. 发布验收

在未经修改的官方构建中，必须同时满足：

- 未登录时，除精确白名单外所有入口都 fail closed。
- Desktop 认证前无 backend、WS、remote 能力连接或能力 IPC。
- SSH remote backend 未登录时只能运行受限 auth bridge/runtime owner，且账户密码只走严格 host key 校验的 SSH stdin。
- Docker 未登录时只有 s6 auth runtime，没有 Agent 或能力进程；先前 `running` 状态不会自动拉起 gateway。
- gateway、serve、cron、MCP、worker 和服务脚本不能绕过门禁。
- Session、CSRF 和密码不进入配置、普通文件、argv、env、日志、Renderer 或 crash dump。
- 本地图形环境从 OS vault 保持登录；SSH/headless 重启后要求重新登录。
- 网络、TLS、429、vault、owner 活性连接或周期验证失败不提供额外 grace。
- owner 换代改变 `runtime_instance_id`，任何旧 worker 或 HTTP/WS token 不能复活。
- Linux/macOS/Windows 的 owner IPC 都校验当前 OS 用户身份，没有普通文件/TCP 回退。
- Desktop 的 local/remote connection scope 相互隔离，切换时不发生跨连接授权或误锁。
- memory API 无 Session 时由服务端独立拒绝。
- 账户分发仅存在于服务器管理员流程；所有客户端 surface 和 Broker 都没有注册、创建账户或自助找回能力。

## 17. 冗余审计

最终设计只保留与真实旁路对应的机制：

- 四个 Python 模块；`store.py` 并入 `runtime.py`。
- 一份状态机和一个 `require_authorized()` 原语。
- Desktop 专用 bridge；TUI 复用既有 RPC。
- 一个 runtime 协议、一个活性连接机制、两种私有 secret owner；其他进程不访问 Django、不写 Cookie。
- `state + epoch + valid_until + boot_id + runtime_instance_id` 五个权威字段；PID 与检查时间仅诊断。
- manifest 只存交互策略，scanner 只负责发现；人工入口列表不成为第三份真值。
- 单一退出码 `20` 加 reason code，不维护多套失败码。
- Docker 复用现有 s6，不增加独立 supervisor。
- 不预建长期设备令牌、Session 转发、离线、多账户或通用 secret-provider 插件框架。

下列机制不能合并或删除：

- owner/lease 防止子进程请求风暴与 Cookie 多写。
- `runtime_instance_id + epoch` 撤销已经发放的 worker 权限，并防止 owner 换代后 token 复活。
- Desktop `guardedIpc` 保护 Electron 主进程的文件、终端和窗口特权。
- backend HTTP/WS epoch 校验保护 Renderer 绕过 IPC 直连 backend 的路径。
- manifest 与 scanner 分别承担声明和发现，删除任一方都会让新入口静默绕过或让清单腐烂。

## 18. 实施边界与顺序

后续实现计划应拆为可独立验证的阶段：

1. Django Session JSON 端点、不可滑动绝对过期与服务端 memory API 鉴权测试。
2. Python `client.py`、统一 `runtime.py`、`guard.py`、VaultOwner/MemoryOwner 与跨平台 IPC。
3. CLI import 期白名单、入口 manifest/scanner 和共享安全边界。
4. Electron bootstrap、connection-scoped 登录、默认拒绝 IPC 与 HTTP/WS token。
5. Ink TUI、ACP、后台入口和 `locked-waiting`。
6. 先审计并收紧 Desktop SSH host key 策略，再实现 SSH auth bridge、MemoryOwner 与 remote backend。
7. 把 Docker auth runtime 接入现有 s6，并修改 `container_boot.py` 禁止未登录 autostart。
8. 跨入口、跨进程、跨连接和跨平台发布验收。

任何阶段都不得以临时明文 Cookie、环境变量 Session、未认证兼容路径或“先启动 backend 再在 UI 隐藏”的方式过渡。
