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
技能、目标、工作流和知识分组。除非安装时为服务选用已有 Agent，
否则也会创建蓝图中的 Agent；之后对蓝图的编辑
不会改动已安装的工作区（而是以可选升级的形式呈现）。

## 一个蓝图包含什么 {#what-a-blueprint-contains}

| 部分 | 内容 |
| --- | --- |
| 清单（Manifest） | Slug、标题、摘要、标签、分类、作者、变更日志、可选的使用指南 |
| 契约（Contract） | 安装环境必须提供的内容：变量、渠道、所需工具/MCP 服务器 |
| 内嵌内容（Embedded） | 完整的技能与 Agent 定义（含工具绑定和初始记忆）、知识包 |
| 配方（Recipe） | 运营模型、订阅、定时任务、工作流、目标、自定义字段 |
| 策略（Policy） | 审批模式、显式 allow/ask/deny 规则、预算和风险限制与安装后检查 |

导出内容经过脱敏：ID、凭证和运行时状态会被剔除，
并有递归扫描拒绝任何包含疑似密钥键名的载荷。
导出会包含工作区中启用的工作流绑定以及工作区范围的工作流图。
知识分组默认只导出结构；操作者可显式选择 Markdown 或 CSV starter，
但只有标记为公开、扫描干净且不含 PII 的知识文本符合条件。工作流中的
固定文档引用必须指向已包含的 starter；安装时将其可移植 key 映射为新的
本地 Document。缺失或有歧义的依赖会被拒绝，运行时输出引用仍保留为模板。浏览器会话
需求只会来自被导出工作区配置实际引用的 provider、label 或会话引用。

## 导出与安装 {#exporting-and-installing}

- **导出**：在工作区页面上，**Export as Blueprint（导出为蓝图）**
  会基于工作区当前配置创建一个草稿蓝图
  （`POST /api/v1/workspaces/{id}/export-blueprint`）。
  自托管导出会下载可移植 JSON。在 Workspaces 导入 JSON、填写声明变量、
  选择当前 Entity 的渠道配置并执行 setup 预检后安装。预检不创建工作区；
  正式安装会再次检查准备状态和权限。
  创建者可以填写使用指南，说明从何开始及如何查看工作结果。
  如需重新冻结同一工作区生成的可编辑草稿，可使用相同 slug 并选择
  **Replace an existing draft**（`replace_existing: true`）。已发布或
  待审核的载荷不能被替换。
- **安装**：`POST /api/v1/blueprints/{id}/install` 创建一个新
  工作区。有两种模式：`simulate`（沙箱化的 `[SIM]` 工作区，用于
  试运行——上线前先检视模拟报告）和 `live`。安装时选择一个
  审批预设（`automatic`、`confirm_changes`、`risk_based` 或
  `follow_default`）。默认是 `automatic`，但显式审批规则、工作流人工
  review 节点、权限、预算、风险限制和生命周期检查仍然有效。
  `follow_default` 使用当前账户默认值，不冻结作者的审批偏好。
  如果作者填写了使用指南，安装者进入新工作区时会看到它，
  之后也可以从工作区顶部再次打开；未填写则不显示指南入口。
  对每项服务，可以选用自己有权访问的任意活跃 Agent，默认使用
  蓝图中的 Agent。两个安装端点都接受 `agent_ids_by_service_key`
  （例如 `{"research": "<your-agent-id>"}`）。定时自动化会绑定到
  该服务选定的本地 Agent，而不是作者的 Agent ID。Agent 选择
  仅属于本次安装的本地设置，不会导出为 ID。
- **校验工作区副本**：工具可调用
  `POST /api/v1/workspaces/{id}/export-blueprint-payload`，获取经过验证的
  可移植配置而不创建草稿。若要在当前 Entity 安装该准确导出，在
  `POST /api/v1/blueprints/install-payload` 的 `payload` 旁传入
  `workspace_source: {workspace_id, export_options}`。服务端会重新检查
  来源权限及生命周期，并与最新导出的完整载荷比较。被修改或已过时的
  内容在创建工作区前返回 HTTP 409；需重新导出再试。准备状态、权限和
  激活门槛仍然有效。这会创建独立副本，不建立已发布来源的升级关联。
- 渠道和浏览器会话**绝不**自动创建——它们会成为
  安装待办事项，由操作者审慎地逐一完成，因为它们涉及
  凭证。
- **提升**：模拟工作区可在预检确认其所需渠道和会话
  均已存在后提升为正式工作区。
- **分享**：所有者可为未发布的蓝图创建可撤销的分享链接。
  创建链接时填写测试者邮箱，可将预览和安装范围限制为已登录、
  邮箱经过验证且与名单匹配的账户。名单留空时保持原有行为：
  任何已登录且持有链接的用户均可安装蓝图。轮换链接会使旧 URL
  失效；撤销链接会终止链接访问。所有者所在实体的成员仍保留
  原有的工作区和蓝图访问权限。

公开 practical examples 是可导入示例，不是默认市场条目；精选商业产品包
不在自托管分发范围。catalog Agent/Skill、工具、浏览器及知识依赖需在目标
实例满足，JSON 不会复制其权限、凭据、执行 grant 或 live consent。
内置 Skill 引用按准确的打包来源身份解析，不能仅凭相同 slug 匹配。
客户交付只携带选定配置和显式选择的 starter；接收方工作区保持暂停且
`provider_access: none`，由接收方在本地完成配置。


## 版本与升级 {#versioning-and-upgrades}

两个版本号，刻意分开：

- **格式版本**（`blueprint_version`，例如 `1.1`）——载荷的 schema 版本。
  旧格式在加载时自动迁移。
- **内容版本**（semver，从 `1.0.0` 起）——仅当内容指纹确实
  发生变化时才在发布时递增。已安装的工作区会与之比较，
  并显示"有可用更新"的徽标。

升级采用"先规划再应用"的方式并带有还原点：安装后你从未
编辑过的条目会被安全覆盖；你自定义过的内容则保留为
你的版本。安装时选用的 Agent 会记录为本地选择；如果蓝图后来
改变该服务，请先检查这项选择，再决定是否替换。
`POST /api/v1/workspaces/{id}/blueprint/revert` 回滚上一次
升级。

源版本升级要求存在可解析且已发布的蓝图。没有合法来源的原始 JSON 安装
不能通过任意新版 JSON 原地升级；draft/archived 也不是升级目标。相同 slug
不能作为绑定另一来源的证明。未记录来源 ID 的历史安装同样保持无来源，
不会自动按名称补绑。配置还原点不能替代部署备份。

## API 摘要 {#api-summary}

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/blueprints`, `GET /{id}` | 列表（自有 + 已发布 + 内置）与查看详情 |
| `PUT /{id}`, `DELETE /{id}` | 编辑草稿、删除 |
| `POST /{id}/install`, `POST /install-payload` | 安装（模拟或正式） |
| `POST /install-payload-preflight` | 不创建工作区，检查原始 JSON 的变量和目标依赖 |
| `POST/DELETE /{id}/share-token`, `GET /shared/{token}` | 分享 |
| `POST /{id}/favorite` | 收藏 |
| `GET /api/v1/blueprints/governance-presets` | 预设定义 |
| `POST /api/v1/workspaces/{id}/export-blueprint` | 导出 |
| `POST /api/v1/workspaces/{id}/export-blueprint-payload` | 获取经过验证的可移植配置，不创建草稿 |
| `GET /api/v1/workspaces/{id}/simulation-report` | 试运行报告 |
| `GET/POST /api/v1/workspaces/{id}/blueprint/upgrade`, `POST .../blueprint/revert` | 升级流程 |

带审核与付费上架的托管市场存在于 Manor 的云服务
产品中；自托管部署通过导出和分享链接交换蓝图。
