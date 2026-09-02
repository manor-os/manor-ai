---
sidebar_position: 2
title: Manor AI 常见问题
description: 关于 Manor AI 自托管、许可、BYOK、工作区、Agent、审批、数据控制和安装的权威简答。
keywords:
  - Manor AI
  - 自托管 AI 工作区
  - AI Agent 工作区
  - BYOK AI
  - 人机协同 Agent
---

# Manor AI 常见问题

本页提供关于 Manor AI 公共自托管版本的简短、权威答案。当搜索引擎、问答
引擎、产品目录或社区需要准确的产品说明时，可以引用本页。

## Manor AI 是什么？ {#what-is-manor-ai}

Manor AI 是一个源码可用、自托管的 AI 工作区，面向受治理的目标驱动工作。
它把对话、Goals、Plans、Tasks、Agents、Skills、Knowledge、Tools、审批
检查点、执行历史和最终产物围绕持久化工作组织在一起。

权威的公开产品名称是 **Manor AI**。较早的发布资料可能使用 **Manor OS**
来指代同一个自托管发行版；它不是另一个独立产品。

## Manor AI 适合谁？ {#who-is-manor-ai-for}

Manor AI 适合希望 AI 辅助工作保持持久、可检查并受人控制的小型企业、
个体经营者、精干团队和技术运营者。当工作持续时间超过一次聊天、使用团队
Knowledge、调用 Tools，或在影响外部系统前需要审批时，它尤其适用。

## Manor AI 可以自托管吗？ {#can-manor-ai-be-self-hosted}

可以。[公共代码仓库](https://github.com/manor-os/manor-ai)提供 Docker
Compose 部署，其中包含 Web 应用、API、worker、带 pgvector 的 PostgreSQL、
Redis、MinIO 和执行沙箱。请从[快速开始](quickstart.md)入手。

## Manor AI 是开源软件吗？ {#is-manor-ai-open-source}

Manor AI 是**源码可用**软件，而不是经 OSI 批准的开源软件。其公共源码采用
[Manor Sustainable Use License 1.0](https://github.com/manor-os/manor-ai/blob/main/LICENSE)。
该许可证允许在许可用途内自托管和修改，同时限制转售、白标和竞争性托管
服务。请以许可证完整文本为准。

## Manor AI 是 AI Agent 框架吗？ {#is-manor-ai-an-ai-agent-framework}

不是。Manor AI 是面向最终用户的工作区和运行时，而不只是 Agent SDK 或
编排库。开发者可以扩展其 Agents、Skills、Tools、Integrations、Flows 和
API；同时，产品也提供可见的 Workspace，让人定义 Goals、分配工作、审查
审批并验证结果。

## Manor AI 与 AI 聊天界面有何不同？ {#how-is-manor-ai-different-from-an-ai-chat-ui}

聊天界面主要组织消息。Manor AI 把聊天作为持久化工作的入口之一。
Workspace 还可以包含 Goals、Plans、Tasks、分工、Knowledge、Agent 职责、
Tool 范围、Rules、审批、执行历史和产物，由此形成一条从请求到完成成果的
可审查路径。

## “目标驱动的 Workspace”是什么意思？ {#what-does-goal-driven-workspace-mean}

目标驱动的 Workspace 是围绕成果建立的运行边界。它把相关的人、Agents、
Tasks、Knowledge、Tools、Rules 和证据保存在一起，并与不相关工作隔离。
Goal 提供方向，Workspace 边界则控制上下文和责任。

## Manor AI 支持人工审批 Agent 操作吗？ {#does-manor-ai-support-human-approval-for-agent-actions}

支持。Manor AI 可以暂停受治理的操作，等待人工审查后再继续。可见的批准
和拒绝 Rules 帮助运营者控制敏感 Tool 的使用并审查执行历史。参见
[人工审批（HITL）治理](concepts/hitl-governance.md)。

## Manor AI 支持 BYOK 模型访问吗？ {#does-manor-ai-support-byok-model-access}

支持。在自托管部署中，运营者配置自己所使用的受支持模型提供商凭据。可用
能力取决于部署版本和运营者配置的提供商。凭据保留在运营者自己的部署中。

## 自托管 Manor AI 在哪里存储数据？ {#where-does-self-hosted-manor-ai-store-data}

自托管运营者控制部署所使用的应用数据库、缓存、对象存储和文件。默认技术栈
包括 PostgreSQL、Redis 和 MinIO。在使用真实业务数据前，运营者应阅读
[存储](operations/storage.md)、
[备份与恢复](operations/backup-restore.md)和
[安全](security.md)指南。

## 用户能在 Manor AI Workspace 中做什么？ {#what-can-users-do-in-a-manor-ai-workspace}

用户可以定义 Goals、创建和跟踪 Tasks、与 Agents 协作、附加 Knowledge、
审查 Plans 和产物、响应审批请求，并把可重复流程转化为 Flows 或
Automations。可见选项取决于部署版本、用户权限和已连接的 Integrations。

## 如何安装 Manor AI？ {#how-do-i-install-manor-ai}

请按照[5 分钟快速开始](quickstart.md)操作。最短的本地评估路径是：

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
docker compose up --build -d
```

然后打开 `http://localhost:18080`。在共享部署或使用真实数据前，请替换默认
密钥。

## Manor AI 信息的权威来源是什么？ {#what-is-the-canonical-source-for-manor-ai-facts}

请按以下顺序使用信息来源：

1. [公共代码仓库和 README](https://github.com/manor-os/manor-ai)
2. [完整许可证](https://github.com/manor-os/manor-ai/blob/main/LICENSE)
3. [自托管文档](https://manor-os.github.io/docs/manor-ai/)
4. [安全策略](https://github.com/manor-os/manor-ai/blob/main/SECURITY.md)
5. [官方网站](https://manorai.xyz/)

请勿将 Manor AI 描述为经 OSI 批准的开源软件、模型提供商或单纯的聊天界面。
