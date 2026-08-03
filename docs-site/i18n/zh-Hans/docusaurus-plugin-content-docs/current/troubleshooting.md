---
sidebar_position: 12
title: 故障排查
---

# 故障排查

自顶向下排查：先执行 `docker compose ps`——大多数问题都是某一个不
健康的服务，而每个服务都定义了健康检查。

## Web 应用无法加载 {#the-web-app-does-not-load}

```bash
docker compose ps
docker compose logs web --tail=100
docker compose logs api --tail=100
```

确认 Web 应用可通过 `http://localhost:18080` 访问。如果 `web` 已启动
但页面报错，检查浏览器控制台中失败的 `/api` 调用——web 容器会把
`/api` 代理到 API 容器。

## 登录失败 {#login-fails}

检查：

- API 日志（`docker compose logs api --tail=200`）。
- `JWT_SECRET_KEY` 已设置且自会话签发以来未变更（轮换它会使所有会话
  失效）。
- 数据库迁移已完成（它们在 API 启动时运行——在 API 日志中查找
  Alembic 相关行）。
- 本地演示登录（`demo@manor.local`）所需的种子数据存在。

## API 无法连接数据库 {#api-cannot-connect-to-database}

检查 `DATABASE_URL`、`DATABASE_URL_SYNC` 和 PostgreSQL 的健康状态：

```bash
docker compose logs postgres --tail=100
docker compose exec postgres pg_isready -U manor
```

如果 API 日志显示的是负载下的连接池超时错误，请回顾
[配置](configuration#database)中的连接容量说明。

## Worker 任务 / 定时任务不运行 {#worker-jobs--scheduled-jobs-do-not-run}

```bash
docker compose logs redis --tail=100
docker compose logs worker --tail=200
docker compose logs worker-work --tail=200
```

两个 worker 都必须在运行：`worker` 负责调度与分发（Celery beat），
`worker-work` 执行长计划步骤。如果定时任务触发了但步骤始终不启动，
要检查的是 `worker-work`。

## Agent 能回答但搜索一无所获 {#agent-answers-but-search-finds-nothing}

知识搜索依赖嵌入（embedding）运行时：

```bash
docker compose logs ollama --tail=50
docker compose logs ollama-init --tail=50
```

`ollama-init` 必须已完成其一次性的 `mxbai-embed-large` 下载。在此
之前，RAG 会降级为纯文本匹配。在慢速网络上首次启动可能需要几分钟。

## 文件或知识上传失败 {#file-or-knowledge-uploads-fail}

检查 MinIO 和 JuiceFS：

```bash
docker compose logs minio --tail=100
docker compose logs juicefs-init --tail=100
docker compose logs api --tail=200
```

`juicefs-init` 必须已成功退出；API 和 worker 将文件系统挂载在
`/mnt/manor`。非正常停止后卡死的 FUSE 挂载会在 API/worker 日志中
表现为 I/O 错误——重启受影响的容器即可。

## Webhook / 渠道消息始终收不到 {#webhooks--channel-messages-never-arrive}

- `PUBLIC_BASE_URL` 必须是服务商的服务器真正能访问到的 URL（生产环境
  须为 HTTPS）。本地测试时使用隧道并更新该值。
- Telegram：当 `PUBLIC_BASE_URL` 不是 HTTPS 时，`TELEGRAM_MODE=auto`
  会回退到轮询——此时应在 API 日志中查找轮询器而不是 webhook。
- Meta/Discord/Twilio 的 webhook 在签名不匹配时会静默失败——确认
  `.env` 中的应用密钥与服务商控制台一致。

## 升级后前端过期 {#stale-frontend-after-an-upgrade}

如果部署后 UI 立即报错，浏览器可能持有过期的资源包。强制刷新
（`Cmd+Shift+R` / `Ctrl+Shift+R`）。自托管运维者应确认 `web` 镜像
确实被重新构建了
（`docker compose build web && docker compose up -d web`）。

## 紧急情况：部署过载 {#emergency-deployment-overloaded}

设置 `DEGRADED_MODE=true` 并重启 API，以卸载高开销路由（流式对话、
沙箱、媒体生成、大文件上传），同时保持登录和读取可用——参见
[配置](configuration#degraded-mode)。

## Docusaurus 文档构建失败 {#docusaurus-docs-build-fails}

从仓库根目录执行：

```bash
cd docs-site
npm ci
npm run build
```

失效链接会导致构建失败。修复链接或更新 `sidebars.js`。
