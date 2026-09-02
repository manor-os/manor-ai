---
title: 什么是目标驱动的 AI 工作区？
description: 目标驱动的 AI 工作区的实用定义，以及它与聊天界面、Agent 框架和工作流自动化工具的区别。
keywords:
  - 目标驱动的 AI 工作区
  - AI Agent 工作区
  - 自托管 AI 工作区
  - 人机协同 AI
  - 受治理的 AI Agent
---

# 什么是目标驱动的 AI 工作区？

**目标驱动的 AI 工作区**是一种持久化的运行环境，让人和 AI Agent
围绕一个成果组织工作。它把目标、计划、任务、知识、工具、权限、审批、
执行历史和最终产物保存在一起，而不是散落在彼此割裂的聊天线程和自动化
运行中。

在 Manor AI 中，Workspace 是这项工作的边界。Goal 提供方向，Tasks、
Agents、Skills、Knowledge、Tools、Rules 和审批则让执行过程可见、可控。

## 简短定义 {#the-short-definition}

> 目标驱动的 AI 工作区把预期成果转化为持久、可审查的工作，并由人与
> 受治理的 Agent 在共享上下文中共同完成。

关键不只在于 **AI**，也在于 **workspace**：上下文、责任、行动和证据
能够长期保持关联的地方。

## 与相邻工具的区别 {#how-it-differs-from-adjacent-tools}

| 产品类型 | 核心单位 | 通常缺少的能力 |
| --- | --- | --- |
| AI 聊天界面 | 对话 | 持久工作状态、归属关系、审批策略和执行证据 |
| Agent 框架 | 代码和 Agent 运行 | 供人分配、审查和治理工作的完整操作界面 |
| 工作流自动化 | 触发器和预定义步骤 | 灵活的目标理解和共享 Agent 上下文 |
| 目标驱动的 AI 工作区 | 成果和持久工作 | 把操作界面、Agent 执行与人的控制边界结合起来 |

这些类别可以协同使用。目标驱动的 Workspace 可以用聊天作为入口、用
Agent 作为执行者、用工作流作为可重复的执行路径。Workspace 将它们连接
到一个持久的业务成果。

## 运行闭环 {#the-operating-loop}

一个实用的目标驱动 Workspace 应提供清晰可见的闭环：

1. 人把预期成果定义为 Goal。
2. Goal 被转化为 Plan 和持久化的 Tasks。
3. Tasks 获得相关的 Knowledge、Agent、Skills 和范围受限的 Tools。
4. Agents 在 Workspace 的 Rules 和权限范围内执行。
5. 敏感操作在人工审批检查点暂停。
6. 状态、证据、评论、执行历史和产物始终与工作关联。
7. 人审查结果，并改进 Plan、自动化或 Workspace。

与一连串孤立提示词相比，这个闭环让长期工作更容易恢复和检查。

## Workspace 边界为何重要 {#why-the-workspace-boundary-matters}

Agent 需要的不只是指令。它还需要知道哪些上下文属于当前任务、可以使用
哪些 Tools、哪些操作需要审批，以及结果应存放在哪里。Workspace 提供了
这层运行边界。

拆分 Workspace 也有助于避免不相关的 Goals、Knowledge、凭据、Rules 和
执行历史混在一起。这既是一种组织模型，也是一层治理界面。

## 为什么人工审批应属于运行时 {#why-human-approval-belongs-in-the-runtime}

在提示词中要求 Agent“先询问”并不等同于强制执行检查点。受治理的
Workspace 可以在敏感 Tool 执行前暂停操作，并把请求展示给人审查。因此，
批准和拒绝 Rules 是执行过程的一部分，而不是提示词中的一句提醒。

参见[人工审批（HITL）治理](/concepts/hitl-governance)，了解 Manor AI
中可见的批准与拒绝模型。

## 何时使用目标驱动的 AI 工作区 {#when-to-use-a-goal-driven-ai-workspace}

当工作符合以下情况时，这种模式尤其有用：

- 持续时间超过一次对话；
- 涉及多个 Tasks 或 Agents；
- 依赖共享或上传的 Knowledge；
- 调用外部 Tools 或 Integrations；
- 产出文件、报告或其他持久化产物；
- 敏感操作前需要人工审批；或
- 需要可审查的记录来说明发生了什么以及原因。

对于一次性问题，聊天界面可能已经足够。对于具有责任、状态和实际影响的
可重复工作，Workspace 提供了缺失的运行层。

## Manor AI 如何实现这一模式 {#how-manor-ai-implements-the-model}

Manor AI 是目标驱动 AI 工作区的一种源码可用、自托管实现。其可见产品
界面包括 Workspaces、Goals、Plans、Tasks、Agents、Skills、Knowledge、
Flows、Automations、审批、执行历史和产物。自托管部署可以使用运营者配置的
模型提供商凭据，并让应用技术栈保持在运营者的控制之下。

你可以从 [Manor AI 常见问题](/faq)、
[5 分钟快速开始](/quickstart)或
[公共代码仓库](https://github.com/manor-os/manor-ai)开始了解。
