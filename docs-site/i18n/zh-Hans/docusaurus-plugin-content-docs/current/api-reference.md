---
sidebar_position: 10
title: API 参考
---

# API 参考

Manor AI 对外暴露的 HTTP API 与 Web 应用使用的完全相同。交互式的
OpenAPI 视图仍然是精确请求与响应 schema 的唯一权威来源，但本页为运维人员和
集成开发者提供了一份可读性更强的公开接口地图。

## 本地 URL {#local-urls}

通过 Docker Compose 运行时：

```text
http://localhost:18080/api/docs
http://localhost:18080/api/redoc
http://localhost:18080/api/openapi.json
```

直接运行 API 时：

```text
http://localhost:8000/api/docs
http://localhost:8000/api/redoc
http://localhost:8000/api/openapi.json
```

## 认证 {#authentication}

大多数 `/api/v1/*` 路由都需要 bearer token：

```http
Authorization: Bearer <access_token>
```

使用 `POST /api/v1/auth/login` 创建会话 token，或通过 Web 应用登录后，
用同一后端查看 OpenAPI 文档。模型提供商 API 密钥则是另一回事：它们是
Agent 运行时使用的 BYOK 凭据，通过 `/api/v1/api-keys` 或设置界面进行管理。

```bash
curl -sS http://localhost:18080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@manor.local","password":"manor-demo"}'
```

## 核心资源 {#core-resources}

| 领域 | 主要端点 | 适用场景 |
| --- | --- | --- |
| 认证与个人资料 | `POST /api/v1/auth/login`、`GET /api/v1/auth/me`、`GET /api/v1/entities/me` | 登录、查看当前用户、管理当前实体 |
| 工作区 | `GET /api/v1/workspaces`、`POST /api/v1/workspaces`、`GET /api/v1/workspaces/{workspace_id}` | 创建运营工作区、更新工作区元数据、读取工作区仪表盘 |
| 工作区运行时 | `GET /api/v1/workspaces/{workspace_id}/operating-model`、`GET /api/v1/workspaces/{workspace_id}/governance`、`GET /api/v1/workspaces/{workspace_id}/activity`、`GET /api/v1/workspaces/{workspace_id}/capabilities` | 配置 Agent、目标、规则和审批在工作区内的行为方式 |
| 聊天 | `POST /api/v1/chat/message`、`POST /api/v1/chat/stream`、`GET /api/v1/chat/conversations` | 向 Agent 运行时发送消息、附加上下文、流式返回响应、管理会话 |
| 工作区聊天 | `GET /api/v1/workspaces/{workspace_id}/chat/messages`、`POST /api/v1/workspaces/{workspace_id}/chat/messages` | 在工作区范围的会话线程中发送和处理消息 |
| Agent | `GET /api/v1/agents`、`POST /api/v1/agents`、`POST /api/v1/agents/generate`、`GET /api/v1/agents/{agent_id}/tools` | 创建 Agent、从提示词生成 Agent、绑定工具 |
| 技能 | `GET /api/v1/skills`、`POST /api/v1/skills`、`POST /api/v1/skills/generate`、`POST /api/v1/skills/install-github` | 管理可供 Agent 使用的可复用技能 |
| 任务 | `GET /api/v1/tasks`、`POST /api/v1/tasks`、`GET /api/v1/tasks/{task_id}` | 跟踪工作、审批、评论、自动化日志和任务状态 |
| 目标与计划 | `GET /api/v1/goals`、`POST /api/v1/goals`、`GET /api/v1/plans`、`POST /api/v1/plans/{plan_id}/approve`、`GET /api/v1/executions` | 定义目标、运行计划、批准待审计划、查看 Agent 执行状态——参见[目标与计划](concepts/goals) |
| 工作流 | `GET/POST /api/v1/workflows`、`POST /api/v1/workflows/{id}/run`、`GET /api/v1/workflows/runs`、`POST /api/v1/workflows/webhook/{token}` | 构建、部署、触发和查看节点图自动化——参见[工作流](concepts/workflows) |
| 定时任务 | `GET/POST /api/v1/jobs`、`POST /api/v1/jobs/{job_id}/run_now`、`GET /api/v1/jobs/{job_id}/runs` | 带运行历史的周期性自动化——参见[自动化](concepts/automations) |
| 记忆 | `GET/POST /api/v1/memories`、`POST /api/v1/memories/extract` | 持久化的 Agent 与工作区记忆——参见[记忆](concepts/memories) |
| 报告 | `GET /api/v1/reports/tasks`、`/usage`、`/activity`、`POST /api/v1/reports/email` | 按需生成 HTML/JSON 报告——参见[报告](concepts/reports) |
| 搜索 | `GET /api/v1/search?q=` | 对任务、文档、Agent、会话的全局子串搜索 |
| 蓝图 | `GET /api/v1/blueprints`、`POST /api/v1/blueprints/{id}/install`、`POST /api/v1/workspaces/{id}/export-blueprint` | 打包并安装工作区配置——参见[蓝图](concepts/blueprints) |
| 日历与预约 | `GET/PUT /api/v1/calendar-settings`、`POST .../booking-links`、`GET .../public/booking-links/{slug}` | 工作时间、预约链接、日程——参见[日历与预约](concepts/calendar-booking) |
| 浏览器会话 | `POST /api/v1/browser/sessions`、`POST .../{id}/navigate`、`.../action` | 服务端 Chromium 自动化——参见[浏览器会话](concepts/browser-sessions) |
| 渠道与配对 | `/api/v1/channels/*` webhook、`POST /api/v1/channel-pairings`、`GET/POST /api/v1/messages` | 入站消息渠道、身份配对、内部私信——参见[消息渠道](integrations/channels) |
| 文档 | `GET /api/v1/documents`、`POST /api/v1/documents/upload`、`GET /api/v1/shared-doc/{token}` | 上传、创建、分享文档并管理文档权限 |
| 集成 | `GET /api/v1/integrations/mcp-servers`、`POST /api/v1/integration-sessions/start`、`GET /api/v1/webhooks` | 连接 MCP 服务器、外部账户、OAuth/Nango 流程和出站 webhook |
| Worker 与沙箱 | `GET /api/v1/workers`、`POST /api/v1/workers/heartbeat`、`POST /api/v1/workspaces/sandbox` | 注册 worker，并在已配置的环境中运行基于沙箱的执行 |
| 运维 | `GET /health`、`GET /health/ready`、`GET /health/deep`、`GET /api/v1/backup/summary`、`GET /api/v1/usage/summary` | 监控就绪状态、导出备份数据、查看用量 |

由于 Web 应用是 API 优先的，公开的 OpenAPI schema 目前包含数百条路由。
建议先从上表所列领域入手；当你需要精确的字段级契约时，再使用 Swagger 或
ReDoc。

## 常用调用 {#common-calls}

### 登录并保存 token {#sign-in-and-keep-the-token}

```bash
TOKEN="$(
  curl -sS http://localhost:18080/api/v1/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"demo@manor.local","password":"manor-demo"}' \
    | jq -r .access_token
)"
```

### 列出工作区 {#list-workspaces}

```bash
curl -sS http://localhost:18080/api/v1/workspaces \
  -H "Authorization: Bearer $TOKEN"
```

### 创建工作区 {#create-a-workspace}

```bash
curl -sS http://localhost:18080/api/v1/workspaces \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Customer Support Operations",
    "description": "Triage customer requests and escalate sensitive work.",
    "category": "operations"
  }'
```

### 发送非流式聊天消息 {#send-a-non-streaming-chat-message}

```bash
curl -sS http://localhost:18080/api/v1/chat/message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Summarize the workspace priorities for today.",
    "workspace_context": true
  }'
```

### 流式获取 Agent 响应 {#stream-an-agent-response}

`/api/v1/chat/stream` 返回 Server-Sent Events，并接受
`multipart/form-data`，因此调用方可以附带文件和可选的工作区上下文。

```bash
curl -N http://localhost:18080/api/v1/chat/stream \
  -H "Authorization: Bearer $TOKEN" \
  -F "message=Draft a support triage plan" \
  -F "workspace_context=true"
```

### 添加模型提供商密钥 {#add-a-model-provider-key}

```bash
curl -sS http://localhost:18080/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenRouter production",
    "provider": "openrouter",
    "api_key": "sk-or-...",
    "default_model": "openai/gpt-4.1",
    "is_default": true
  }'
```

原始密钥只在创建或轮换时被接受。列表响应仅暴露密钥的元数据和前缀，
不会返回密钥本身。

## 公开与嵌入路由 {#public-and-embed-routes}

有些路由专为未认证的外部访问者设计，前提是运维人员先创建了分享或公开
token：

| 场景 | 端点 |
| --- | --- |
| 分享文档 | `/api/v1/shared-doc/{token}`、`/content`、`/download` |
| 分享文件夹 | `/api/v1/shared-folder/{token}` |
| 公开任务评审 | `/api/v1/public/task`、`/update-status`、`/complete`、`/evaluate` |
| 公开聊天挂件 | `/api/v1/public/chat/{token}`、`/session`、`/message`、`/message/stream`、`/embed.js` |
| 渠道 webhook | `/api/v1/channels/*` 回调端点 |
| 工作流 webhook | `/api/v1/workflows/webhook/{token}` |
| 公开预约 | `/api/v1/calendar-settings/public/booking-links/{slug}`、`.../book` |

请将分享 token 和渠道 webhook 密钥视为凭据。一旦泄露，请立即轮换。

## 生成 OpenAPI JSON {#generate-openapi-json}

```bash
make openapi
```

该命令会在开发目录中将 OpenAPI 文档写入 `docs/openapi.json`。
生成的 schema 可用于查阅、契约测试和客户端代码生成。

```bash
npx openapi-typescript docs/openapi.json -o manor-api.d.ts
```

## 稳定性说明 {#stability-notes}

- **核心资源**中列出的路由是自托管部署进行集成的推荐起点。
- 平台管理路由在常规的工作区自动化中并不需要。
- 云端市场、计费、远程编码和 CLI 分发相关的路由不属于公开的 OSS 运行时
  导出范围。
- OpenAPI schema 随仓库一起做版本管理。升级 Manor AI 后请重新生成客户端。
