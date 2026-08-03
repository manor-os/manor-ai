---
sidebar_position: 1
title: 快速上手路径
description: 从克隆仓库到跑起一个 Manor AI 工作区的最短路径。
---

# 快速上手路径

验证 Manor AI 作为自托管 AI 工作区运行时所需的全部步骤:克隆仓库、启动本地
服务栈、登录、添加模型密钥、查看预置工作区,并确认受治理的操作会暂停等待
人工审批。

<img
  src="img/manor-ai-runtime.png"
  alt="Manor AI 工作区运行时,展示运行中的工作区仪表盘"
/>

## 这是什么(简述)

Manor AI 是一个自托管的 AI 工作区运行时。一个工作区把目标、任务、文档、
知识、Agent、工具和人工审批规则集中在一处,让运营者清楚看到 Agent 被允许
做什么。

第一次运行应当验证四件事:

| 概念 | 应该看到什么 |
| --- | --- |
| 工作区 | 目标、任务、知识、规则、Agent 映射集中在一个运营视图里 |
| 运行时 | API、worker、PostgreSQL、Redis、MinIO、沙箱等服务协同运行 |
| 治理 | 自然语言规则映射为审批与拒绝的动作模式 |
| API 面 | FastAPI 端点与 Web 应用使用的完全一致 |

## 开始之前

安装 Docker Compose v2、Git、Python 3.11+ 和 Node.js 20+。本地评估可直接
复制示例环境文件;任何共享部署都必须先替换生成的密钥并配置好凭据,再邀请
用户。

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
```

> 共享部署前请先修改密钥。演示账号和默认 `.env.example` 值仅供本地评估。

## 5 分钟路径

镜像在本地就绪后,预计 5-10 分钟。

<div className="ma-path-table">

| 步骤 | 页面或命令 | 应该看到什么 | 耗时 |
| --- | --- | --- | --- |
| 1 | `docker compose up --build -d` | 核心容器变为 healthy | 2-5 分钟 |
| 2 | `http://localhost:18080` | 登录页和预置演示账号 | 1 分钟 |
| 3 | 设置 | 模型密钥保存在你的部署中 | 1-2 分钟 |
| 4 | 工作区 | 任务、目标、文档和运行评分 | 1 分钟 |
| 5 | 治理 | 敏感操作在工具执行前需要审批 | 1 分钟 |

</div>

### 1. 启动服务栈

```bash
docker compose up --build -d
```

这会启动 Web 应用、API、worker、带 pgvector 的 PostgreSQL、Redis、MinIO
和沙箱服务。

### 2. 打开 Manor AI

打开本地 Web 应用:

```text
http://localhost:18080
```

自托管模式会预置一个本地演示账号:

```text
demo@manor.local / manor-demo
```

### 3. 添加模型密钥

打开设置,为你想使用的模型路径添加一个提供商密钥。自托管部署下 Manor AI
采用 BYOK(自带密钥)模式,模型凭据只保存在你自己的部署中。

### 4. 查看工作区

打开工作区视图,查看运行评分、目标、任务、文档和 Agent 映射。工作区看起来
应该像一个正在运转的系统,而不是空空如也的 SDK 示例。

<img
  src="img/manor-ai-goals.png"
  alt="Manor AI 目标执行画布,展示目标与工作区任务的关联"
/>

### 5. 检查治理

打开工作区规则。发外部消息、发社交帖等敏感操作可以要求人工审批;破坏性
操作可以在工具执行前直接拒绝。

<img
  src="img/manor-ai-governance.png"
  alt="Manor AI 治理规则:外部消息需审批,破坏性操作被拦截"
/>

## 完成后你将拥有

- 一个 `http://localhost:18080` 的浏览器会话。
- 已登录的本地演示账号。
- 运行中的 API、worker、数据库、缓存、对象存储和沙箱服务。
- 一个可见任务、目标和治理规则的工作区。
- 同一部署内可访问的本地 API 文档。

<img
  src="img/manor-ai-api-reference.png"
  alt="Manor AI OpenAPI 参考与认证端点"
/>

## 下一步

- [安装](installation.md):本地与部署环境的前置条件。
- [配置](configuration.md):全部环境变量参考——密钥、模型提供商、存储、
  限流与渠道。
- [工作区与知识](concepts/workspaces-knowledge.md)、
  [Agents](concepts/agents.md)、[任务](concepts/tasks.md)、
  [目标与计划](concepts/goals.md):核心运营模型。
- [工作流](concepts/workflows.md)与[自动化](concepts/automations.md):
  节点图自动化与定时自动化。
- [人工审批治理](concepts/hitl-governance.md):Agent 触达外部系统前的审批
  与拒绝策略。
- [消息渠道](integrations/channels.md):把 WhatsApp、Telegram、Slack、
  邮件等接入你的 Agent。
- [API 参考](api-reference.md):Web 应用所用 HTTP API 的地图。
