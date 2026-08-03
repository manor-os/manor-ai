---
sidebar_position: 3
title: 任务
---

# 任务

任务是可追责工作的单位：Agent 处理的任何事情都会成为任务，你分配给同事的任何工作也会成为任务。一个任务包含状态、优先级（1–5）、负责人（某个人、某个指定 Agent，或在未分配时的主 Agent）、截止时间、评论、附件、子任务，以及——当由 Agent 运行时——完整的执行记录。

## 创建任务 {#creating-tasks}

- **看板**：**Tasks → New Task**。
- **聊天**：向 Agent 提出请求——*"创建一个明天的任务，跟进 Hayes 的预订"*。
- **渠道**：入站消息（邮件、WhatsApp 等）会在其路由到的工作区中创建任务。
- **自动化与工作流**：定时任务和工作流运行会创建它们所处理的任务。
- **预订链接**：确认的预订会成为一个任务。

## 状态与看板 {#statuses-and-the-board}

任务状态：`created`、`proposed`、`pending`、`scheduled`、`in_progress`、`waiting_on_customer`、`on_hold`、`blocked`、`completed`、`cancelled`、`failed`——并强制执行合法的状态转换（非法转换返回 409）。`GET /api/v1/tasks/constants` 返回完整的状态机。

看板将状态分为五列：

| 列 | 状态 |
| --- | --- |
| 待办 | created, pending, proposed |
| 已排期 | scheduled |
| 进行中 | in_progress |
| 需要关注 | waiting_on_customer, on_hold, blocked, failed |
| 已完成 | completed, cancelled |

拖拽卡片即可变更状态。看板与列表模式、可见列以及列顺序都是按用户保存的偏好，并跨设备同步。内置日历标签页和 CSV 导入。集合（带图标和颜色的分类）可在整个看板上对任务分组。

## 任务详情 {#task-detail}

任务页面展示：属性（状态、优先级、负责人、截止时间、分类、请求者）、Markdown 描述、评论（Markdown + 附件；对等待中任务的评论会将其恢复，@ 提及某个 Agent 会将其派发）、子任务、生成的输出文件，以及执行时间线——来自 Agent 运行的计划步骤、工具调用和状态变更。

如果在创建时就分配了 Agent，运行会立即开始。`POST /api/v1/tasks/{task_id}/retry` 重新运行失败的任务（复用或重新生成其计划）；等待你决定的审批步骤可以直接在任务中处理——参见[人工审批（HITL）治理](hitl-governance.md)。

## 在 Manor 之外共享工作 {#sharing-work-outside-manor}

无需认证、以访问码限定范围的任务端点，让外部协作者不必注册账号也能更新任务：为任务生成一个会话访问码后，持码者可以查看任务、更新状态、完成任务或留下客户评价（`/api/v1/public/task/...`，访问码 7 天后过期）。

## SLA 策略 {#sla-policies}

定义 SLA 策略（`/api/v1/tasks/sla-policies`）并将其附加到任务上；违约会在任务和报表中显现。

## API 摘要 {#api-summary}

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/tasks`、`POST /api/v1/tasks` | 列表（支持状态/工作区/分类筛选）与创建 |
| `GET /api/v1/tasks/board` | 带计数的看板分组 |
| `GET/PUT /api/v1/tasks/board-preferences` | 按用户的看板偏好 |
| `POST /api/v1/tasks/{id}/move` | 状态转换（带校验） |
| `GET/PUT/DELETE /api/v1/tasks/{id}` | 读取 / 更新 / 删除 |
| `POST /api/v1/tasks/{id}/retry`、`/hitl-response`、`/approval` | Agent 运行控制与审批 |
| `GET/POST /api/v1/tasks/{id}/logs`、`/attachments`、`GET /{id}/history` | 评论、文件、审计 |
| `GET/POST /api/v1/tasks/categories` | 集合 |
| `GET/POST /api/v1/tasks/templates`、`POST .../{id}/instantiate` | 支持 `{{variable}}` 渲染的任务模板（仅 API 提供） |
