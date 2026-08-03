---
sidebar_position: 4
title: 目标与计划
---

# 目标与计划

目标是可衡量的业务成果；计划则是 Agent 工作被分解、执行和审计的方式。二者共同构成了从"我们想要什么"到"实际执行了什么"的链条：

```text
Goal  ←→  Task  →  ExecutionPlan  →  ExecutionStep
```

一个目标关联着推进它的任务；由 Agent 处理的任务会获得一个执行计划；计划是由类型化步骤组成的 DAG，由工作进程执行。

## 目标 {#goals}

目标存在于工作区中（或在实体层级），并包含：

- 一个**指标**：`metric_key`（例如 `follower_count`、`mrr`）、基线值、当前值和目标值，以及可选的截止时间。
- **进度节奏**：`on_track`、`behind`、`ahead`、`at_risk`、`achieved` 或 `unknown`——根据测量值与目标的对比计算得出。
- **状态**：`active`、`achieved`、`abandoned`、`paused`。当进度达到目标时，目标会自动切换为 `achieved`，其测量计划随之停止。
- **测量**：可选的测量来源和频率；测量以[定时任务](automations.md)的形式运行，并追加到可绘制成图表的时间序列中。

目标以卡片形式出现在工作区的**目标**标签页中，带有进度节奏徽章和进度条，还有一个可平移缩放的目标图，渲染完整的目标 → 任务 → 计划步骤链条。任务通过贡献类型（`direct`、`indirect`、`discovered`）和预估影响关联到目标，因此工作区能够展示每项工作*为什么*存在。

Agent 可以自行创建并关联目标——在工作区聊天中说一句"跟踪一个 6 月前达到 1,000 名新闻通讯订阅者的目标"就足够了。

## 计划与步骤 {#plans-and-steps}

当 Agent 接手一个任务时，规划器会生成一个**执行计划**：一个带依赖关系的步骤 DAG，其中每个步骤有类型（`llm`、`action`、`subagent`、`code`、`human`、`sleep`、`parallel_fanout`、`gather`）、在派发时解析为具体 Agent 的服务键、风险级别和成本追踪。步骤输出通过引用供给后续步骤。

计划状态沿 `draft → running → completed` 流转，另有 `pending_approval`（计划在执行前需要人工签核）、`paused`、`needs_attention`、`failed`、`cancelled` 和 `replanned`（执行器生成了后继计划；血缘关系会被保留）。步骤级的 `waiting_human` 是[人工审批（HITL）](hitl-governance.md)暂停执行的地方。

所有内容都可以从任务详情的执行时间线或通过 API 查看。

## API 摘要 {#api-summary}

| 端点 | 用途 |
| --- | --- |
| `GET/POST /api/v1/goals`、`GET/PUT/DELETE /api/v1/goals/{id}` | 管理目标 |
| `GET/POST /api/v1/goals/{id}/measurements` | 测量时间序列 |
| `GET /api/v1/plans` | 列出计划（按工作区、任务、状态） |
| `GET /api/v1/plans/{id}`、`GET /api/v1/plans/{id}/steps` | 查看计划 |
| `POST /api/v1/plans/from-task/{task_id}` | 为任务调用规划器 |
| `POST /api/v1/plans/{id}/approve` / `/cancel` | 人工签核 / 停止 |
| `POST /api/v1/plans/{id}/retry-failed-steps`、`POST /api/v1/plans/steps/{step_id}/retry` | 恢复；重试 waiting-human 步骤会记录该审批 |

目标模板（`GET /api/v1/goal-templates`）提供现成的配方——每日简报、邮件分流、X/Twitter 增长等——一次调用即可创建目标及其种子任务和测量计划（`POST /api/v1/workspaces/{workspace_id}/apply-template`）。
