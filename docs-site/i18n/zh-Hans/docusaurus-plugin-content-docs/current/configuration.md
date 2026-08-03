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

## 数据库 {#database}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://manor:manor_secret@postgres:5432/manor` | API 与 worker 使用的异步 SQLAlchemy URL。 |
| `DATABASE_URL_SYNC` | `postgresql://manor:manor_secret@postgres:5432/manor` | Alembic 迁移使用的同步 URL,须与 `DATABASE_URL` 指向同一数据库。 |
| `DATABASE_POOL_SIZE` | `5` | 每个 API 进程保持的连接数。 |
| `DATABASE_MAX_OVERFLOW` | `2` | 突发负载下允许超出池大小的额外连接数。 |
| `DATABASE_POOL_TIMEOUT` | `10` | 等待空闲连接的秒数,超时报错。 |
| `DATABASE_POOL_RECYCLE` | `1800` | 池中连接的回收周期(秒)。 |

连接池默认值按单机部署设定。若调高 `API_WORKERS`,注意每个 worker 进程有
独立连接池:总连接数 ≈ `API_WORKERS × (DATABASE_POOL_SIZE +
DATABASE_MAX_OVERFLOW)` 再加 Celery worker。总和须低于 PostgreSQL 的
`max_connections`。

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

## 限流 {#rate-limits}

所有限流器默认关闭(便于本地评估)。任何面向真实用户的共享部署都应启用。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RATE_LIMIT_ENABLED` | `false` | 通用 API 限流总开关。 |
| `API_RATE_LIMIT_REQUESTS` | `200` | 每窗口每客户端允许的请求数。 |
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
| `JWT_EXPIRE_MINUTES` | `1440` | 会话有效期(默认 24 小时)。 |
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
| `PUBLIC_BASE_URL` | `http://localhost:8010` | 外部提供商可访问到本部署的基础 URL。用于构建 webhook 与 OAuth 回调地址,以及签发公开文件 URL(`/api/v1/fs/public/{token}`,视频提供商生成图生视频时会拉取)。生产环境必须为 `https://`。 |
| `APP_URL` | `http://localhost:18080` | 浏览器访问的 Web URL。默认 Compose 的 `web` 服务监听 18080 并将 `/api` 代理到 API 容器。 |

本地测试 webhook 时,使用可信 HTTPS 隧道并在隧道存活期间把
`PUBLIC_BASE_URL` 指向隧道地址。

## 功能开关 {#feature-rollout}

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLOWS_AVAILABLE` | 未设置 | 工作流上线开关。本地/开发环境默认启用 Flows;`MANOR_ENV` 为 `prod`/`production` 时导航项显示 **Soon**,直到设为 `true`。 |
| `MANOR_PREVIEW_INTEGRATIONS` | 未设置 | 逗号分隔的提供商 key(如 `facebook,gmail`),在非生产部署上提前展示"即将上线"的集成,供测试用户使用。 |
| `MANOR_PREVIEW_CHANNELS` | 未设置 | 同上,针对消息渠道。 |

`DEPLOYMENT_MODE=oss` 时 `MANOR_ENV` 默认为 `local`,因此标准自托管安装
无需额外配置即启用 Flows。

## 渠道集成 {#channel-integrations}

每个提供商都需要在其开发者门户注册 OAuth 应用,回调 URL 设为
`{PUBLIC_BASE_URL}/api/v1/integrations/oauth/{server_key}/callback`。

| 变量 | 说明 |
| --- | --- |
| `TELEGRAM_MODE` | 入站模式:`webhook`(需 HTTPS;保存集成时自动注册)、`polling`(异步 `getUpdates` 轮询,无需公网 URL)或 `auto`(默认——`PUBLIC_BASE_URL` 为 https 时用 webhook,否则轮询)。 |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth 应用。 |
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` | LinkedIn OAuth 应用。 |
| `X_CLIENT_ID` / `X_CLIENT_SECRET` | X(Twitter)OAuth 应用。 |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` | Slack OAuth 应用。 |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | Notion OAuth 应用。 |
| `QUICKBOOKS_CLIENT_ID` / `QUICKBOOKS_CLIENT_SECRET` | QuickBooks OAuth 应用。 |
| `MS_CLIENT_ID` / `MS_CLIENT_SECRET` | 一个 Azure AD 应用注册即可覆盖 Outlook、OneDrive、Microsoft 日历、Teams 和 Excel。重定向 URI 设为上述回调地址;所需 Microsoft Graph 委托权限列表见 `.env.example`。 |
| `MS_TENANT` | `common`(工作/学校 + 个人账号)、`organizations`、`consumers`,或指定 Azure AD 租户 GUID。 |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | Discord OAuth 客户端。 |
| `DISCORD_PUBLIC_KEY` | Ed25519 公钥(hex),用于校验交互签名。 |
| `DISCORD_BOT_TOKEN` | 机器人发消息时必需。 |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | 通过 Twilio 提供短信、语音与 WhatsApp。 |
| `DEEPGRAM_API_KEY` | Twilio Media Streams 语音通话的流式语音转文字。 |
| `OPENAI_API_KEY` | 语音通话的文字转语音,同时供 `/audio/speech` 复用。 |

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
| `NANGO_WEBHOOK_SECRET` | 未设置 | 任意 32+ 字符随机串。Manor 启动钩子会把同一值写入 Nango,使其外发 webhook 携带签名;在 `/api/v1/nango/webhook` 接收时校验。 |
| `NANGO_WEBHOOK_URL` | 未设置 | Nango POST webhook 的目标 URL。默认为 Compose 内部地址 `http://api:8000/api/v1/nango/webhook`;生产环境需覆盖。 |

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

需要在各平台开发者门户登记的重定向 URI 为
`${NANGO_PUBLIC_URL}/oauth/callback`。`.env.example` 内附 HubSpot 与
Facebook/Instagram 的完整示例,以及各平台开发者控制台的入口链接。

何时使用 Nango、何时使用第一方 OAuth 应用,见
[Nango 集成](integrations/nango)。
