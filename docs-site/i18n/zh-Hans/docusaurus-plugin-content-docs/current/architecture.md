---
sidebar_position: 6
title: 架构
---

# 架构

Manor AI 是一个 monorepo，包含 Python 后端、React 前端、worker 运行时
以及隔离的执行服务。

```text
React web app
    |
    v
FastAPI API  <----> PostgreSQL + pgvector
    |                 Redis
    |                 MinIO + JuiceFS
    |                 Ollama (embeddings)
    v
Celery workers (control plane + work queue)
    |
    +---- Sandbox service (per-run containers)
    +---- Integrations, channels, and webhooks
```

## 后端 {#backend}

API 位于 `apps/api`。它暴露工作区、聊天、Agent、任务、目标、
工作流、文档、知识、集成、渠道、调度和
运维等端点。

共享的领域逻辑位于 `packages/core`：

- SQLAlchemy 模型
- 服务层
- Agent 运行时（agentic 循环、工具、提示词上下文）
- 工作流引擎（节点图运行器、导入器）
- 技能
- 权限
- 迁移
- Celery 任务

## 前端 {#frontend}

Web 应用位于 `apps/web`。它是一个使用 Vite 和
TypeScript 构建的 React 18 单页应用。在 Docker 中它通过 web 代理与 API 通信，
在本地开发时则直接与 API 通信。

## Worker 运行时 {#worker-runtime}

后台执行拆分为两个共享同一镜像的 Celery worker：

- **控制平面**（`worker`）：运行 Celery beat，触发
  [定时任务](concepts/automations.md)，派发工作租约，并
  监控部署。队列为 `celery`。
- **工作队列**（`worker-work`）：执行耗时较长的 Agent 计划步骤和工作流
  运行。队列为 `work`。

这种拆分可以防止少数长时间运行的 Agent 步骤阻塞
调度。两个 worker 都挂载实体文件系统，并共享 API 的
代码库和配置模型。

## 隔离边界 {#isolation-boundaries}

- [沙箱服务](operations/sandbox.md)在按运行分配的容器中执行代码，
  容器带有内存/CPU/PID 限制——绝不在 API 或 worker
  进程中执行。
- 实体文件系统路径通过 JuiceFS 上的 Manor 文件服务进行
  作用域限定。
- 工具访问受 Agent 设置和
  [人工审批（HITL）治理](concepts/hitl-governance.md)约束。
- 一切都以实体为作用域（OSS 模式下每个部署为单租户），
  实体内部再由工作区级权限管控。

## 数据存储 {#data-stores}

| 存储 | 角色 |
| --- | --- |
| PostgreSQL（+pgvector） | 结构化数据与分块 embedding 的单一事实来源 |
| Redis | 缓存、Celery broker/结果、限流、JuiceFS 元数据 |
| MinIO | 对象存储：上传文件、产物、JuiceFS 数据块 |
| JuiceFS | 由 Redis 元数据 + MinIO 数据块组装而成的 POSIX 实体文件系统 |
| Ollama | 用于文档 RAG 的本地 embedding 模型（`mxbai-embed-large`） |

运维细节参见[存储](operations/storage.md)。
