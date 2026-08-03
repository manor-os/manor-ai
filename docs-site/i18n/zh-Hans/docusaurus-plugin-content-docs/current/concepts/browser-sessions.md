---
sidebar_position: 11
title: 浏览器会话
---

# 浏览器会话

浏览器会话为 Agent（以及操作者）提供一个服务端 Chromium 浏览器，
用于 Web 自动化：导航、点击、填写表单、运行 JavaScript、
截图、提取页面内容以及渲染 PDF。

在侧边栏打开 **Browser Sessions（浏览器会话）**，即可创建会话、操控它，
并通过截图观察它看到的内容。

## 能力 {#capabilities}

每个会话支持：

- `navigate`——加载一个 URL（30 秒超时）。
- `screenshot`——PNG 截图，可选整页截图。
- 动作——`click`、`fill`、`evaluate`（JavaScript）、`get_content`
  （页面 HTML）、`pdf`。

会话保存在 API 进程的内存中：它们是工作工具，而非
持久化记录。任何值得保留的内容（截图、提取的内容）
都应保存到文档或任务附件中。

## 前置要求 {#requirements}

服务端浏览基于 Playwright 与 Chromium，属于可选
依赖。如果未安装，API 会返回明确的错误；安装
方式：

```bash
pip install playwright && playwright install chromium
```

（在 Docker 部署中，如果要使用该功能，请将此步骤加入你的 API 镜像。）

## API 摘要 {#api-summary}

| 端点 | 用途 |
| --- | --- |
| `POST /api/v1/browser/sessions` | 创建（`{"headless": true}`） |
| `GET /api/v1/browser/sessions`, `GET .../{id}` | 列表 / 查看详情 |
| `POST /api/v1/browser/sessions/{id}/navigate` | 跳转到某个 URL |
| `POST /api/v1/browser/sessions/{id}/screenshot` | 截图 |
| `POST /api/v1/browser/sessions/{id}/action` | click / fill / evaluate / get_content / pdf |
| `DELETE /api/v1/browser/sessions/{id}` | 关闭 |

请将浏览器自动化视为敏感能力：它可以触达你的服务器
能触达的一切。只将它授予需要它的 Agent，并为对外
动作搭配[人工审批（HITL）治理](hitl-governance.md)。
