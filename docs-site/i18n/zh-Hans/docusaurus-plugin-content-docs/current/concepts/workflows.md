---
sidebar_position: 5
title: 工作流
---

# 工作流

工作流是节点图自动化——与 Agent 并列的一等原语。Agent 在运行时自行决定步骤，而工作流执行的是你设计好的图：在需要确定性的地方保持确定性，在需要智能的地方使用 `llm` 和 `agent` 节点。

在侧边栏打开 **Flows** 即可使用编辑器。工作流定义是可复用且与上下文无关的；你通过绑定将它*部署*到某个工作区，每次执行都会记录为一次运行，带有完整的分步历史。

## 定义、绑定、运行 {#definitions-bindings-runs}

| 对象 | 含义 |
| --- | --- |
| **定义** | 可复用的图：命名的步骤、连接、变量和触发器类型。定义属于你的实体，可跨工作区共享。 |
| **绑定** | 将定义部署到一个运行上下文——通常是一个工作区加一个触发器（`manual`、`webhook`、`schedule`、`event` 或 `workspace_event`）。一个定义可以有多个绑定，每个绑定有自己的变量和启用状态。运行时由绑定的工作区提供连接器、知识、审批人和预算。 |
| **运行** | 一次执行。捕获启动时定义的快照、每步结果、脱敏后的执行追踪、重试血缘和最终状态。 |

运行状态沿 `pending → running → paused → completed / failed / cancelled` 流转。`paused` 表示运行正在等待——审批、计时器或事件——并且可以被恢复。

## 构建工作流 {#building-a-workflow}

获得一张图的四种方式：

1. **空白画布**——从可搜索的节点面板添加节点，连接它们，在侧边面板中配置每个节点。
2. **模板**——内置的起点，例如每日摘要或客服邮件分流。
3. **导入**——引入来自 **n8n**、**Dify** 或 **ComfyUI** 的现有导出文件；Manor 会把它们的节点类型映射到自己的节点类型上。
4. **AI 编辑**——用自然语言描述自动化，让 AI 生成或修改图。在你保存之前不会持久化任何内容。

每个工作流必须有且仅有一个入口节点——`trigger` 或 `webhook`——没有入口节点时保存会被拒绝。你可以在运行整个图之前单独测试任意一个节点（"run node"）。

值通过 `{{stepId.output}}` 模板变量在步骤之间流动；工作流级变量以 `{{varName}}` 形式可用，触发器负载则是 `{{trigger}}`。

## 节点类型 {#node-types}

| 类别 | 节点 |
| --- | --- |
| 入口 | `trigger`、`webhook` |
| AI | `llm`、`rag`（知识检索）、`agent`（完整 Agent 循环）、`classifier`、`extract` |
| 动作 | `tool`、`connector`（一次集成调用——Gmail、Notion、Slack 等）、`code`、`http`、`notify`、`respond` |
| 逻辑 | `condition`（IF）、`switch`、`loop`、`parallel`、`merge`、`wait`、`stop`、`end` |
| 数据 | `transform`、`filter`、`aggregate`、`datetime`、`split`、`limit`、`sort`、`dedupe`、`extractfromfile` |
| 媒体 | `image`、`video`、`audio`、`media` |
| 编排 | `subworkflow`、`foreach_subworkflow`（对列表逐项映射子运行） |
| 状态与审批 | `workflow_project`（跨运行共享的持久状态）、`workflow_action_grant`（持久的审批范围） |
| 注释 | `note`（画布注释，运行时跳过） |

`agent` 节点运行与聊天相同的 Agent 循环：为它提供提示词、可选的系统提示词、工具列表、模型和轮次上限——或将其指向某个工作区服务，由该工作区分配的 Agent 处理这一步。

## 最小示例 {#a-minimal-example}

内置的"Daily digest"模板只有三个工作步骤：

```json
[
  { "id": "t", "type": "trigger", "name": "Daily trigger", "next": ["r"] },
  { "id": "r", "type": "rag", "name": "Gather updates",
    "config": { "query": "this week's updates", "limit": 10 }, "next": ["l"] },
  { "id": "l", "type": "llm", "name": "Summarize",
    "config": { "prompt": "Summarize into a short digest:\n{{r.output}}" }, "next": ["n"] },
  { "id": "n", "type": "notify", "name": "Post digest",
    "config": { "channel": "slack", "message": "{{l.output}}" }, "next": ["e"] },
  { "id": "e", "type": "end", "name": "Done", "next": [] }
]
```

## 触发器 {#triggers}

| 触发器 | 触发方式 |
| --- | --- |
| `manual` | Run 按钮，或 `POST /api/v1/workflows/{id}/run`。 |
| `webhook` | `POST /api/v1/workflows/webhook/{token}`——公开端点，token 是绑定的共享密钥。运行内联执行，因此 `respond` 节点可以向调用方返回同步的 HTTP 响应。 |
| `schedule` | 由绑定创建的定时任务，由平台调度器触发。 |
| `event` / `workspace_event` | 平台事件通过 `POST /api/v1/workflows/trigger` 路由到匹配且已启用的绑定。 |

绑定还可以注册为其工作区的**聊天入口**：当一条聊天消息明确匹配该工作流的用途时，工作区聊天会根据消息预填工作流的输入并启动一次运行，运行进度会在执行过程中投射回会话中。

## 审批与人机协同 {#approvals-and-human-in-the-loop}

带 `wait_type: "approval"` 的 `wait` 节点会暂停运行，并向运行所有者展示一张审阅卡片（可附带渲染后的负载）。恢复运行需要运行所有者的明确决定；其他任何操作都会被拒绝。计时器等待在时间较短时内联休眠，较长时则持久化后重新入队；事件等待会一直暂停，直到被恢复。

后续的 `workflow_action_grant` 节点可以将一次已批准的等待转换为持久的、有时限的授权，这样同一工作流的重复运行就不会为已在该范围内批准过的操作再次发起提示。

## 运行、重试与可观测性 {#runs-retries-and-observability}

每次运行都会存储其定义快照、每步输出和脱敏后的执行追踪（密钥已清除、大输出已摘要、产物和子运行已关联）。运行详情视图通过 SSE 实时流式展示节点状态。

- **取消 / 恢复**：`POST /api/v1/workflows/runs/{run_id}/cancel` 和 `/resume`。
- **重试**：`/retry` 重启失败的运行；`from_step_id` 从检查点重试，复用未改变的先前步骤的结果。如果定义在原始运行之后发生了变化，检查点重试会被拒绝（HTTP 409）。
- **血缘**：`GET /runs/{run_id}/family` 返回重试链。
- 对可缓存的节点类型，未改变步骤的结果可以在运行之间复用（类似 ComfyUI），因此对图尾部的迭代非常快。

## API 摘要 {#api-summary}

均在 `/api/v1/workflows` 之下：

| 端点 | 用途 |
| --- | --- |
| `GET` / `POST` `""`、`GET/PUT/DELETE /{id}` | 管理定义 |
| `POST /ai-edit` | AI 生成或编辑图（SSE 流） |
| `POST /run-node` | 单独执行一个节点 |
| `POST /import` | 导入 n8n / Dify / ComfyUI 导出文件 |
| `GET/POST /bindings`、`PUT/DELETE /bindings/{id}` | 管理部署 |
| `POST /bindings/{id}/run`、`POST /{id}/run`、`POST /{id}/run-stream` | 启动运行（内联、异步或 SSE 流式） |
| `POST /trigger`、`POST /webhook/{token}` | 事件和 webhook 入口 |
| `GET /runs`、`GET /runs/{run_id}`、`GET /runs/{run_id}/family` | 运行历史 |
| `POST /runs/{run_id}/cancel` / `resume` / `retry`、`POST /runs/{run_id}/step` | 运行控制 |

## 可用性 {#availability}

Flows 在本地和开发环境中默认启用。在生产环境中，导航项显示为 **Soon**，直到部署设置 `FLOWS_AVAILABLE=true`——参见[配置](../configuration#feature-rollout)。
