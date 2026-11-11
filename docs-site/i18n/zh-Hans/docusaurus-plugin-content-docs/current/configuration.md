---
sidebar_position: 4
title: 配置
---

# 配置

配置全部通过环境变量读取。从 `.env.example` 开始:

```bash
cp .env.example .env
```

`.env.example` 中的每个变量都在本页有文档,分组与文件一致。表中默认值即
`.env.example` 中的值;"未设置"表示默认为空,对应功能在填入前保持关闭。

## 真实部署必改项 {#required-for-real-deployments}

将 Manor AI 暴露到本地评估之外前,务必修改:

| 变量 | 用途 |
| --- | --- |
| `JWT_SECRET_KEY` | 签名用户会话。使用足够长的随机值。 |
| `DATABASE_URL` | 异步 PostgreSQL 连接串。 |
| `DATABASE_URL_SYNC` | Alembic 使用的同步 PostgreSQL 连接串。 |
| `REDIS_URL` | Redis 缓存与 broker 地址。 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 对象存储凭据。 |
| `PUBLIC_BASE_URL` | Webhook 与生成媒体回调使用的公网 URL。 |
| `APP_URL` | 浏览器访问的 Web URL。 |

## 部署模式与默认模型 {#deployment-mode-and-model-defaults}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEPLOYMENT_MODE` | `oss` | `oss` 即自托管模式。所有自托管部署保持 `oss` 不变。 |
| `LLM_MODEL` | `anthropic/claude-sonnet-4` | 默认模型 id。用户和实体可在设置中覆盖。 |

自托管 Manor AI 采用 BYOK(自带密钥):模型调用只使用在**设置**中配置的
提供商凭据。不要把模型 API 密钥写进镜像或源码仓库。

<img
  src="img/manor-byok.png"
  alt="Manor AI 模型设置中的 BYOK 配置"
/>

## 实时语音通话 {#live-voice-calls}

普通 Chat、Workspace Chat 和公开 Webchat 的麦克风输入按钮旁提供**开始语音通话**。
接通后可连续说话，停顿后 AI 自动回复，回复按句生成并播放；再次开口即可打断播放。
通话面板包含字幕、静音和挂断。对话文字保存在原聊天中，工具执行和审批仍走现有聊天运行时。
涉及复杂工具操作时，回复可能需要更长时间。
Workspace 暂停后仍可直接聊天，受暂停限制的操作不会恢复；公开渠道通话要求 Workspace 处于活跃状态。

自托管 Realtime 通话需在“账户 → 模型”为**Primary AI** 配置原生 OpenAI BYOK。
语音识别和语音合成角色的密钥只用于轮次式 STT/Chat/TTS，不会被提升为整段 Realtime 凭据。
服务端自动检测说话、调用现有 Chat，再播放回复，延迟取决于模型与工具执行时间。
公开 Webchat 使用渠道所有者的凭据和计费范围，不会使用访客的密钥。


除 localhost 外，麦克风访问需要 HTTPS。自定义反向代理需为 `/api/v1/audio/live`
允许 WebSocket 升级，并为应用页面设置 `Permissions-Policy: microphone=(self)`；
文档预览框架继续禁止麦克风。嵌入 Webchat 时，宿主页面也需允许聊天来源使用麦克风，
生成的嵌入脚本已包含 iframe 权限。

音频由语音服务商处理，服务端保存对话文字、通过统一语音计费流程记录费用，并在通话中复查权限。
挂断会立即停止采集、取消请求准备并阻止新的重试；已经提交的语音网关请求可能在通话结束后返回，并记录实际用量。

统一语音链路先在本地检测人声，减少背景噪声造成的误识别和误打断。疑似发声只暂时暂停播放，识别出新的话语后才正式打断；空识别会恢复原来的声音。
已完成的回复会保留在通话面板中，不因打断而清空。首句单独合成，后续语音在播放期间准备。
面板分别显示语音识别、Chat 思考和语音生成阶段，并在声音生成前显示 Chat 回复。通话面板显示麦克风音量，
并提供不调用服务商的“测试扬声器”提示音。没有声音时，先检查音量条、扬声器测试、浏览器输出设备和音量，
再检查服务器到语音网关的网络连接。

挂断、离开页面、切换聊天、失去权限或达到 30 分钟上限都会结束通话。
断线后需手动重新呼叫，已完成的操作不会自动重放。
旧的 `/chat/voice-session` 和 `/chat/voice-save` 接口由经过认证的服务端中继取代。

## 数据库 {#database}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://manor:manor_secret@postgres:5432/manor` | API 与 worker 使用的异步 SQLAlchemy URL。 |
| `DATABASE_URL_SYNC` | `postgresql://manor:manor_secret@postgres:5432/manor` | Alembic 迁移使用的同步 URL,须与 `DATABASE_URL` 指向同一数据库。 |
| `DATABASE_POOL_SIZE` | `5` | 每个 API 进程保持的连接数。 |
| `DATABASE_MAX_OVERFLOW` | `2` | 突发负载下允许超出池大小的额外连接数。 |
| `DATABASE_POOL_TIMEOUT` | `10` | 等待空闲连接的秒数,超时报错。 |
| `DATABASE_POOL_RECYCLE` | `1800` | 池中连接的回收周期(秒)。 |
| `DATABASE_ADMISSION_LIMIT` | `0` | 每个进程/事件循环允许的并发 Session 事务数。`0` 对本地连接池和直连 PostgreSQL 的 worker 保留池大小 + overflow − 1，最小为 1；PgBouncer 下取 app `4` / worker `1`，其他应用 `NullPool` 取 `4`。 |
| `DATABASE_MAX_WAITERS` | `14` | 并发事务已满时最多排队的事务数。 |
| `DATABASE_ADMISSION_TIMEOUT` | `2` | 排队等待上限(秒)。过载返回 HTTP 503，包含 `detail.code=database_busy` 和 `Retry-After`。 |
| `DATABASE_HOLD_WARN_SECONDS` | `5` | 连接占用超过此秒数后，记录带 request ID 的警告。 |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `0` | 可选的 PostgreSQL 事务内语句时限(毫秒)。`0` 保持数据库默认值。 |
| `DATABASE_LOCK_TIMEOUT_MS` | `0` | 可选的事务内等待数据库锁时限(毫秒)。 |
| `DATABASE_IDLE_TRANSACTION_TIMEOUT_MS` | `0` | 可选的事务内空闲时限(毫秒)。 |

须计算所有 API/chat 副本与进程：连接池上限为
`sum(副本数 × 进程数 × (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW))`。
另外计入各 Celery 进程的事务预算、迁移/运维连接和安全余量，总和应低于 PostgreSQL 的
`max_connections`。Celery 使用一次性 `NullPool` 引擎；准入名额只在同一事件
循环内共享，不跨 worker 进程，可保守按并发进程数 × 准入上限计算。
直连 PostgreSQL 的 worker 虽然使用 `NullPool`，仍保留原有自动额度（默认池参数下为 6），
避免将已有嵌套 Session 缩减为单名额。这是兼容性默认值，不代表本地连接池。
显式额度为 1 时，调用方持有事务期间仍无法让第二个 Session 同时查询。
`NullPool` 的正数显式额度不受未使用的 `DATABASE_POOL_SIZE/MAX_OVERFLOW` 限制。
PgBouncer 客户端预算应计入副本 × 进程 × 事务额度、独立探针、滚动更新、退出中的 Pod 和 Job；
服务端连接池另行约束 PostgreSQL 总连接数，也须设置后端排队时限。两层容量不能混为一谈。

名额按数据库事务持有，不按整个请求、WebSocket、LLM 调用或文件流持有。
使用实际本地连接池时，自动限制在池容量至少为 2 时，为有时限的就绪探针保留一个名额。显式限制不能
超过池大小 + overflow。过载不会自动重放写入；重试应遵守 `Retry-After`，
并使用对应操作的幂等约定。

启用可选时限前，须审计任务/审批中长时间持锁的流程。设置仅在当前事务内生效，
兼容 PgBouncer，不会终止已经释放数据库连接的慢 LLM 或 sandbox 工作。
自托管模式的 `/health/deep` 提供连接占用/长占用次数、活跃事务、排队/拒绝次数
和最长等待时间。`queue_full` 表示队列已满、尚未进入排队就被拒绝的次数；
`wait_timeouts` 表示已排队但超过准入时限的次数。`peak_active_transactions`
和 `peak_waiters` 是当前进程/事件循环的峰值，不代表整个部署的容量，重启会清零。
长占用日志只记录 request ID，不记录 SQL 参数或凭据。

## Redis {#redis}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REDIS_URL` | `redis://redis:6379/0` | 缓存、Celery broker、限流后端与在线状态存储。 |
| `REDIS_MAXMEMORY` | `0` | Redis 服务内存上限。`0` 为不限制;共享主机建议设置如 `512mb` 的上限。 |
| `REDIS_MAXMEMORY_POLICY` | `noeviction` | 达到上限后的淘汰策略。若能接受缓存淘汰,可将有限的 `REDIS_MAXMEMORY` 搭配 `allkeys-lru`。 |

同一 Redis 实例的 `/1` 库存放 JuiceFS 元数据(见
[存储](#object-storage-and-entity-filesystem));若指向外部 Redis,请保持 `/0` 与
`/1` 分离。

## API 服务调优 {#api-server-tuning}

单机应急控制项。默认值保守、适合评估;调高前先测量 CPU、p95 延迟和数据库
连接数。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_WORKERS` | `1` | Uvicorn worker 进程数。 |
| `API_LIMIT_CONCURRENCY` | `120` | 单 worker 并发连接上限,超出即拒绝请求。 |
| `API_BACKLOG` | `256` | 等待连接的 socket backlog。 |
| `API_TIMEOUT_KEEP_ALIVE` | `5` | Keep-alive 超时(秒)。 |

共享 WebSocket 推送管理器每进程最多同时发送 32 条帧，每次发送和失败连接的
关闭操作分别有 2 秒时限。同一 socket 的发送串行化，组织索引仅定位目标组织的
候选连接，不缓存权限；内容推送前仍校验当前成员关系、可见范围和 Token 版本。
慢连接会被关闭，重连后须与已落库消息对齐。实时推送是尽力交付，不是持久化的
送达回执。这些上限不等于可支持的用户数；调整进程、副本或连接预算前，须一起
压测真实 socket、消息频率、数据库延迟和后台任务。

## 限流 {#rate-limits}

所有限流器默认关闭(便于本地评估)。任何面向真实用户的共享部署都应启用。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RATE_LIMIT_ENABLED` | `false` | 按 IP 限流(`RATE_LIMIT_PER_MINUTE`,默认 `100`)。同时开启 `CHAT_RATE_LIMIT_ENABLED` 时,已登录请求跳过它,改用按用户的额度。 |
| `API_RATE_LIMIT_REQUESTS` | `600` | 每窗口每个已登录用户(未登录时按 IP)允许的 API 请求数。需开启 `CHAT_RATE_LIMIT_ENABLED`。 |
| `API_RATE_LIMIT_WINDOW_SECONDS` | `60` | 通用限流窗口长度。 |
| `CHAT_RATE_LIMIT_ENABLED` | `false` | 聊天路由(成本最高)的独立限流器。 |
| `CHAT_RATE_LIMIT_REQUESTS` | `30` | 每窗口允许的聊天请求数。 |
| `CHAT_RATE_LIMIT_WINDOW_SECONDS` | `60` | 聊天限流窗口长度。 |
| `REDIS_RATE_LIMIT_ENABLED` | `false` | 将限流状态存入 Redis,使限额在所有 API worker 间共享(而非按进程独立)。`API_WORKERS > 1` 时务必启用。 |

## 降级模式 {#degraded-mode}

部署过载时的紧急刹车。`DEGRADED_MODE=true` 时,高成本路由返回 `503`
(`code=degraded_mode`),健康检查、配置、登录和基础读取保持可用。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEGRADED_MODE` | `false` | 总开关。 |
| `DEGRADED_DISABLE_CHAT_STREAM` | `true` | 降级期间禁用流式聊天。 |
| `DEGRADED_DISABLE_SANDBOX` | `true` | 降级期间禁用沙箱执行。 |
| `DEGRADED_DISABLE_MEDIA_GENERATION` | `true` | 降级期间禁用图像/视频生成。 |
| `DEGRADED_DISABLE_LARGE_UPLOADS` | `true` | 降级期间拒绝大文件上传。 |

四个 `DEGRADED_DISABLE_*` 开关仅在 `DEGRADED_MODE=true` 时生效,用于选择
刹车覆盖哪些能力。

## 对象存储与实体文件系统 {#object-storage-and-entity-filesystem}

Manor AI 使用 MinIO 做对象存储、JuiceFS 做实体级文件系统。默认 Compose
栈通过 `juicefs-init` 服务自动格式化并挂载 JuiceFS 卷。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MINIO_ENDPOINT` | `minio:9000` | S3 兼容端点。 |
| `MINIO_ACCESS_KEY` | `minioadmin` | 共享部署必改。 |
| `MINIO_SECRET_KEY` | `minioadmin` | 共享部署必改。 |
| `MINIO_BUCKET` | `manor` | 上传文件与生成产物的桶。 |
| `MANOR_FS_ENABLED` | `true` | 启用按实体隔离的文件系统(Agent 文件工具、知识文件同步)。 |
| `MANOR_FS_ROOT` | `/mnt/manor` | API 与 worker 使用的挂载路径。 |
| `JUICEFS_META_URL` | `redis://redis:6379/1` | JuiceFS 元数据存储,使用 Redis `/1` 库。 |
| `JUICEFS_STORAGE` | `minio` | JuiceFS 数据后端类型。 |
| `JUICEFS_BUCKET` | `http://minio:9000/manor` | JuiceFS 数据块的对象存储桶 URL。 |
| `JUICEFS_ACCESS_KEY` | `minioadmin` | 与 MinIO 凭据保持一致。 |
| `JUICEFS_SECRET_KEY` | `minioadmin` | 与 MinIO 凭据保持一致。 |

## 沙箱 {#sandbox}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SANDBOX_SERVICE_URL` | `http://sandbox:8000` | 隔离代码执行服务的地址。 |
| `SHELL_SANDBOX_ENABLED` | `true` | 允许 Agent 在沙箱内执行 shell 命令。设为 `false` 可完全禁用 shell 执行。 |

沙箱边界内的运行细节见[沙箱运维](operations/sandbox)。

## 认证 {#authentication}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `JWT_SECRET_KEY` | `change-this-to-a-random-string` | 任何共享部署**必须**替换。 |
| `JWT_ALGORITHM` | `HS256` | Token 签名算法。 |
| `JWT_EXPIRE_MINUTES` | `1440` | 访问令牌有效期，最长 24 小时。Remember me 使用独立的 30 天浏览器会话。 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | 未设置 | 启用"使用 Google 登录"。在 Google Cloud Console 创建 Web OAuth 客户端。 |
| `VITE_GOOGLE_CLIENT_ID` / `VITE_GOOGLE_DRIVE_API_KEY` | 未设置 | 启用知识页的 Google Drive 选择器。需在 Google Cloud 项目中启用 Google Picker API 和 Google Drive API。 |

## 邮件(SMTP) {#email-smtp}

外发邮件支撑邀请、验证码、通知与邮件渠道回复。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EMAIL_ENABLED` | `false` | 总开关。没有 SMTP 服务器就保持关闭;需要发信的流程会静默跳过。 |
| `SMTP_HOST` | 未设置 | SMTP 服务器主机名。 |
| `SMTP_PORT` | `587` | 标准 STARTTLS 端口。 |
| `SMTP_USER` / `SMTP_PASSWORD` | 未设置 | SMTP 凭据。 |
| `SMTP_FROM_EMAIL` / `SMTP_FROM_NAME` | 未设置 | 外发邮件的发件地址与显示名。 |
| `SMTP_STARTTLS` | `true` | 使用 STARTTLS 升级连接。 |

## 密钥加密(Vault) {#secrets-encryption-vault}

集成凭据等机密通过 Vault transit 后端做静态加密。Compose 栈自带一个用于
开发式部署的本地 `vault` 辅助服务。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VAULT_TOKEN` | 未设置 | Vault transit 后端的 token。 |
| `VAULT_TRANSIT_KEY` | `manor-keys` | 加解密使用的 transit key 名称。 |

## Agent 工具:搜索与行情数据 {#agent-tools-search-and-market-data}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEARCH_ENGINE` | `serper` | Agent 网络搜索工具后端:`serper` 或 `tavily`。 |
| `SEARCH_API_KEY` | 未设置 | 所选搜索引擎的 API key。未设置时网络搜索工具不可用。 |
| `FINNHUB_API_KEY` | 未设置 | 为生成的仪表盘模块启用实时行情数据。 |

## 公网 URL {#public-urls}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | `http://localhost:8010` | 外部提供商可访问到本部署的基础 URL。用于构建入站 webhook 地址,以及签发公开文件 URL(`/api/v1/fs/public/{token}`,视频提供商生成图生视频时会拉取)。生产环境必须为 `https://`。 |
| `APP_URL` | `http://localhost:18080` | 浏览器访问的 Web URL,也是第一方 OAuth 回调地址的基础。默认 Compose 的 `web` 服务监听 18080 并将 `/api` 代理到 API 容器。 |

本地测试 webhook 时,使用可信 HTTPS 隧道并在隧道存活期间把
`PUBLIC_BASE_URL` 指向隧道地址。

## 功能开关 {#feature-rollout}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLOWS_AVAILABLE` | 未设置 | 自托管稳定 Flows 默认启用，包括 `prod`/`production`；设为 `false` 可显式禁用。Cloud production 保留上线闸口。 |
| `FLOWS_RELEASED` | 未设置 | 自托管默认不显示 **Soon** 标签；显式配置可覆盖默认值。 |
| `MANOR_PREVIEW_INTEGRATIONS` | 未设置 | 逗号分隔的提供商 key(如 `facebook,gmail`),在非生产部署上提前展示"即将上线"的集成,供测试用户使用。 |
| `MANOR_PREVIEW_CHANNELS` | 未设置 | 同上,针对消息渠道。 |

`DEPLOYMENT_MODE=oss` 时 `MANOR_ENV` 默认为 `local`,因此标准自托管安装
无需额外配置即启用 Flows。

## Manor PMS 与 Minutes {#manor-pms}

在 **Integrations → Manor PMS / Manor Minutes** 中选择 **连接**，登录相应应用并
确认授权。完成后返回 Integrations，显示账号和连接状态。无需先在 Apps 安装应用或
复制个人访问令牌；可通过账号菜单测试、重新连接或断开。

运维人员需要在各应用中注册 Manor OAuth 客户端，并在 Manor 配置对应的客户端凭据。
应用的回调白名单必须包含本部署的精确地址：

- PMS：`{APP_URL}/api/v1/integrations/oauth/manor_pms/callback`
- Minutes：`{APP_URL}/api/v1/integrations/oauth/manor_mcp_minutes/callback`

| 变量 | 用途 |
| --- | --- |
| `PMS_OAUTH_CLIENT_ID` / `PMS_OAUTH_CLIENT_SECRET` | PMS 为 Manor 注册的客户端凭据。 |
| `PMS_OAUTH_BASE_URL` | 部署配置的 PMS API 源地址，用于换取令牌、核验身份和撤销授权。 |
| `PMS_OAUTH_AUTHORIZE_URL` | 浏览器访问的 PMS 授权地址，以 `/oauth/authorize` 结尾；前端和 API 地址不同时需单独设置。 |
| `MINUTES_OAUTH_CLIENT_ID` / `MINUTES_OAUTH_CLIENT_SECRET` | Minutes 为 Manor 注册的客户端凭据。 |
| `MINUTES_OAUTH_BASE_URL` | 部署配置的 Minutes API 源地址。 |
| `MINUTES_OAUTH_AUTHORIZE_URL` | 浏览器访问的 Minutes 授权地址，以 `/oauth/authorize` 结尾。 |

在 API 和 worker 环境中配置这些设置。客户端秘密及账号令牌保存在凭据库，不应进入
聊天、任务参数或 Blueprint。应用还需要保留自身 **使用 Manor 登录** 的客户端配置，
以验证应用用户身份。

授权码流程使用 PKCE；连接绑定发起授权的 Manor 用户、组织和应用核验后的身份。
OAuth worker 负责刷新并轮换令牌。部署端点变化或授权失效后需要重新连接。
断开时会立即停用本地工具访问；远端撤销失败时显示错误并允许重试，不能将此状态
当作应用端已经完成撤销。
重新连接也会撤销旧授权；这一步失败时，连接保持停用，“断开连接”会重试清理
两份授权。更改应用 API 地址前应先在原配置下断开；旧凭据不会发送到新地址。

PMS 管理访问要求 owner/admin 账号。44 个工具覆盖物业、租客、租约、维修、付款及
短租业务。调用使用所选连接的令牌，写操作继续遵守 Manor 审批规则。
已保存的手动 PMS 凭据保留以便迁移，新连接采用 OAuth；`PMS_API_URL` 继续配置
部署使用的 PMS 工具网关。

Minutes 工具通过所连接的账号读取会议、转写、摘要和行动项。连接不会绕过会议访问
规则及会议问答计费。`MINUTES_MCP_URL` 用于配置其 HTTP MCP 端点。

启用连接前，应部署配套的应用 OAuth 接口及数据库迁移。新 Manor 身份建立隔离的
应用命名空间；历史物业和会议数据需要运维人员显式维护身份映射，不能靠名称或
邮箱匹配授予访问权限。

升级时，需要由负责启动初始化的 API 服务刷新集成目录和 OAuth 客户端配置。
标准 OSS API 在启动时自动执行；拆分部署需要指定负责初始化的服务
（`MANOR_STARTUP_SIDE_EFFECTS_OWNER=1`）。Chat 和 worker 服务不会自行刷新目录。

## 渠道集成 {#channel-integrations}

每个提供商都需要在其开发者门户注册 OAuth 应用,回调 URL 设为
`{APP_URL}/api/v1/integrations/oauth/{server_key}/callback`。`PUBLIC_BASE_URL`
仅用于入站 webhook。

| 变量 | 说明 |
| --- | --- |
| `TELEGRAM_MODE` | 入站模式:`webhook`(需 HTTPS;保存集成时自动注册)、`polling`(异步 `getUpdates` 轮询,无需公网 URL)或 `auto`(默认——`PUBLIC_BASE_URL` 为 https 时用 webhook,否则轮询)。 |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth 应用。 |
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` | LinkedIn OAuth 应用。 |
| `X_CLIENT_ID` / `X_CLIENT_SECRET` | X(Twitter)OAuth 应用。 |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` | Slack OAuth 应用。 |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | Notion OAuth 应用。 |
| `QUICKBOOKS_CLIENT_ID` / `QUICKBOOKS_CLIENT_SECRET` | QuickBooks OAuth 应用。 |
| `LARK_APP_ID` / `LARK_APP_SECRET` | 国际版 Lark OAuth 应用凭据。 |
| `ROBINHOOD_CLIENT_ID` | Robinhood 官方 Trading MCP 的公开 OAuth 客户端，无需 Client Secret。 |
| `MS_CLIENT_ID` / `MS_CLIENT_SECRET` | 一个 Azure AD 应用注册即可覆盖 Outlook、OneDrive、Microsoft 日历、Teams 和 Excel。重定向 URI 设为上述回调地址;所需 Microsoft Graph 委托权限列表见 `.env.example`。 |
| `MS_TENANT` | `common`(工作/学校 + 个人账号)、`organizations`、`consumers`,或指定 Azure AD 租户 GUID。 |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | Discord OAuth 客户端。 |
| `DISCORD_PUBLIC_KEY` | Ed25519 公钥(hex),用于校验交互签名。 |
| `DISCORD_BOT_TOKEN` | 机器人发消息时必需。 |
| `TWILIO_ALLOW_UNSIGNED_LOCAL` | 仅用于本地 Twilio webhook 调试；staging/production 保持 `false`。Twilio Account SID、Auth Token 和电话号码在每个 Integration 中配置。 |
| `OPENAI_REALTIME_MODEL` | Browser 与 Twilio Voice 共用的原生 OpenAI Realtime 模型，默认 `gpt-realtime`。 |

在“账户 → 模型”为**语音合成**配置提供商 BYOK 凭据。这些角色级凭据遵循 Entity 的模型配置。
缺少密钥或密钥与模型不匹配时，生成会在供应商请求前被拒绝；自托管音频不会
使用部署级托管凭据兜底。


### 国际版 Lark OAuth {#international-lark-oauth}

Lark 用户 OAuth 只支持国际版 Lark 服务。在 [Lark 开发者控制台](https://open.larksuite.com/app)
创建 OAuth 应用，并登记以下精确回调 URI：

`{APP_URL}/api/v1/integrations/oauth/lark/callback`

为应用启用以下用户权限：

```text
offline_access contact:user.base:readonly im:chat:read im:message
im:message.send_as_user im:message.group_msg:get_as_user
im:message.p2p_msg:get_as_user docx:document bitable:app
```

在 API 和 worker 环境中设置 `LARK_APP_ID` 和 `LARK_APP_SECRET`；或者作为自托管
管理员，在本地 OAuth client 配置中保存它们。应用会把本地保存的 secret 存入凭据库；
本地管理员配置优先于环境变量启动初始化的值。

之后，每位用户都在**集成**中为 Lark 选择**连接**，并自行授予其账号所需的访问权限。
连接同时绑定该用户和已注册的 Lark 应用。授权被撤销或过期后，请使用相同账号和应用
重新连接；更换应用凭据后必须重新取得用户授权。历史飞书应用或机器人账号仍是独立
集成，其凭据不能用于国际版 Lark OAuth。

### Robinhood Trading MCP {#robinhood-trading-mcp}

向 Robinhood 的[注册端点](https://agent.robinhood.com/oauth/trading/register)注册公开客户端，
回调必须完整匹配 `{APP_URL}/api/v1/integrations/oauth/robinhood/callback`，
认证方式为 `token_endpoint_auth_method: "none"`，支持 authorization-code 和 refresh-token
授权，scope 为 `internal`。注册是否接受由 Robinhood 决定。将返回的 `client_id` 设为
API 和 worker 环境中的 `ROBINHOOD_CLIENT_ID`，也可在集成目录初始化后，通过
**设置 → 开发者 → Robinhood** 保存。更改环境变量后重启服务。`APP_URL` 必须与注册的部署一致；
不能将仅注册了生产回调的客户端用于 localhost 回调。不需要 `ROBINHOOD_CLIENT_SECRET`。

随后每位用户在 **集成 → Robinhood** 选择 **连接**，自行完成 Robinhood 的账户授权。
Manor 使用 PKCE S256，通过现有凭证库保存令牌，并由 OAuth worker 刷新。
Agent 工具会按已连接用户从 `https://agent.robinhood.com/mcp/trading` 动态发现，
不会使用自行猜测的交易工具参数或静态备用定义。

Robinhood 的 `internal` 权限**并非只读**，可能允许 Agentic 交易。账户授权不等于授权
Manor 下单，外部写操作仍受现有审批策略约束，股票监控请求不会授权交易。
应用注册及账户授权也不包含行情的商用再分发许可。账户要求与支持的行为见
[Robinhood Agentic Trading 说明](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)。

## Nango(SaaS OAuth 聚合) {#nango-saas-oauth-aggregator}

Nango 提供 200+ SaaS 平台的 OAuth 连接。启动内置服务:

```bash
docker compose --profile nango up -d nango-server
```

然后打开 `http://localhost:3003`,设置管理员密码,并将 Secret Key 复制到
`NANGO_SECRET_KEY`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NANGO_BASE_URL` | `http://nango-server:3003` | Nango 服务的内部 URL。 |
| `NANGO_PUBLIC_URL` | 未设置 | OAuth 回调与 SaaS webhook 的公网主机名。本地开发可留空;生产应在反向代理后使用 `https://nango.<你的域名>`。 |
| `NANGO_SECRET_KEY` | 未设置 | 来自 Nango 管理界面。留空则完全禁用 Nango。 |
| `NANGO_PUBLIC_KEY` | 未设置 | Nango 管理界面中的 public key。 |
| `NANGO_CONNECT_HMAC_KEY` | 未设置 | 独立的 64 位小写十六进制密钥，用于 Nango Connect HMAC 和派生 webhook URL 凭据。浏览器 OAuth 必填；仅 webhook 接收器在未改动的本地 Compose 中回退使用 `NANGO_SECRET_KEY`。 |
| `NANGO_WEBHOOK_SECRET` | 未设置 | Compose 兼容保留变量。Nango 0.36 不会使用它为外发 webhook 请求添加签名，不能依赖它进行 webhook 认证。 |
| `NANGO_WEBHOOK_URL` | 未设置 | Nango POST webhook 的目标 URL。Manor 会追加派生的 `nango_webhook_token` 查询参数，因为 Nango 0.36 不发送签名头。默认 Compose 内部地址为 `http://manor-api:8000/api/v1/nango/webhook`；生产环境需覆盖。 |

### 按平台引导提供商 {#per-platform-provider-bootstrap}

想开放某个 Nango 聚合平台,只需添加一组变量,Manor 会在 API 启动时推送进
Nango 管理数据库——无需在 Nango 管理界面点选:

```bash
NANGO_PROVIDER_<PROVIDER>_CLIENT_ID=...
NANGO_PROVIDER_<PROVIDER>_CLIENT_SECRET=...
NANGO_PROVIDER_<PROVIDER>_SCOPES=...      # 可选,空格分隔
NANGO_PROVIDER_<PROVIDER>_KEY=...         # 可选,默认为 <PROVIDER>
NANGO_PROVIDER_<PROVIDER>_PROVIDER=...    # 可选,默认为 <PROVIDER>
```

X/Twitter 启用 Nango 路径时使用 `NANGO_PROVIDER_TWITTER_*`；Manor 内部
仍使用 canonical provider key `twitter_x`。使用 Direct OAuth
(`X_CLIENT_ID`/`X_CLIENT_SECRET`) 的环境不要同时启用这组 Nango 变量。

需要在各平台开发者门户登记的重定向 URI 为
`${NANGO_PUBLIC_URL}/oauth/callback`。`.env.example` 内附 HubSpot 与
Facebook/Instagram 的完整示例,以及各平台开发者控制台的入口链接。

何时使用 Nango、何时使用第一方 OAuth 应用,见
[Nango 集成](integrations/nango)。
