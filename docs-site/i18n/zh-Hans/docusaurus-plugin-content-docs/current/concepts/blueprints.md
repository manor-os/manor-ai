---
sidebar_position: 7
title: 蓝图
---

# 蓝图

蓝图是工作区配置的一份可移植、带版本的快照
——包括 Agent、技能、目标、工作流、定时任务、治理策略和
知识脚手架——可以从一个工作区导出，并在其他地方安装为
一个全新的工作区。

**安装是拷贝，不是链接。** 新工作区会物化出自己的
Agent、技能、目标、工作流和知识分组；之后对蓝图的编辑
不会改动已安装的工作区（而是以可选升级的形式呈现）。

## 一个蓝图包含什么 {#what-a-blueprint-contains}

| 部分 | 内容 |
| --- | --- |
| 清单（Manifest） | Slug、标题、摘要、标签、分类、作者、变更日志 |
| 契约（Contract） | 安装环境必须提供的内容：变量、渠道、所需工具/MCP 服务器 |
| 内嵌内容（Embedded） | 完整的技能与 Agent 定义（含工具绑定和初始记忆）、知识包 |
| 配方（Recipe） | 运营模型、订阅、定时任务、工作流、目标、自定义字段 |
| 策略（Policy） | 治理规则（永不允许 / 需人工审批（HITL）/ 自动批准、预算上限）与安装后检查 |

导出内容经过脱敏：ID、凭证和运行时状态会被剔除，
并有递归扫描拒绝任何包含疑似密钥键名的载荷。

## 导出与安装 {#exporting-and-installing}

- **导出**：在工作区页面上，**Export as Blueprint（导出为蓝图）**
  会基于工作区当前配置创建一个草稿蓝图
  （`POST /api/v1/workspaces/{id}/export-blueprint`）。
- **安装**：`POST /api/v1/blueprints/{id}/install` 创建一个新
  工作区。有两种模式：`simulate`（沙箱化的 `[SIM]` 工作区，用于
  试运行——上线前先检视模拟报告）和 `live`。安装时选择一个
  治理预设（`safe`、`standard`、`aggressive`）。
- 渠道和浏览器会话**绝不**自动创建——它们会成为
  安装待办事项，由操作者审慎地逐一完成，因为它们涉及
  凭证。
- **提升**：模拟工作区可在预检确认其所需渠道和会话
  均已存在后提升为正式工作区。
- **分享**：所有者可以签发一个分享令牌；通过该链接，任何已
  认证用户都可以安装此蓝图，而无需公开发布它。

平台内置五个"单人公司"蓝图，作为不可变的
起点随平台发布。

## 版本与升级 {#versioning-and-upgrades}

两个版本号，刻意分开：

- **格式版本**（`blueprint_version`，例如 `1.1`）——载荷的 schema 版本。
  旧格式在加载时自动迁移。
- **内容版本**（semver，从 `1.0.0` 起）——仅当内容指纹确实
  发生变化时才在发布时递增。已安装的工作区会与之比较，
  并显示"有可用更新"的徽标。

升级采用"先规划再应用"的方式并带有还原点：安装后你从未
编辑过的条目会被安全覆盖；你自定义过的内容则保留为
你的版本。`POST /api/v1/workspaces/{id}/blueprint/revert` 回滚上一次
升级。

## API 摘要 {#api-summary}

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/blueprints`, `GET /{id}` | 列表（自有 + 已发布 + 内置）与查看详情 |
| `PUT /{id}`, `DELETE /{id}` | 编辑草稿、删除 |
| `POST /{id}/install`, `POST /install-payload` | 安装（模拟或正式） |
| `POST/DELETE /{id}/share-token`, `GET /shared/{token}` | 分享 |
| `POST /{id}/favorite` | 收藏 |
| `GET /api/v1/blueprints/governance-presets` | 预设定义 |
| `POST /api/v1/workspaces/{id}/export-blueprint` | 导出 |
| `GET /api/v1/workspaces/{id}/simulation-report` | 试运行报告 |
| `GET/POST /api/v1/workspaces/{id}/blueprint/upgrade`, `POST .../blueprint/revert` | 升级流程 |

带审核与付费上架的托管市场存在于 Manor 的云服务
产品中；自托管部署通过导出和分享链接交换蓝图。
