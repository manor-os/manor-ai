---
sidebar_position: 9
title: 报表
---

# 报表

报表是按需生成、由服务端渲染的部署活动摘要。它们在
每次请求时都从实时数据全新生成——不存储报表，
没有过期的快照。

在侧边栏打开 **Reports（报表）**，即可预览、下载或通过邮件发送它们。

## 报表类型 {#report-types}

| 类型 | 内容 | 默认时间窗口 |
| --- | --- | --- |
| **任务** | 任务量、完成趋势与状态分布 | 30 天 |
| **用量** | 各模型与各 Agent 的 token 与成本用量 | 30 天 |
| **活动** | 各工作区近期活动的摘要 | 7 天 |

每份报表都以带样式的 HTML 生成（自包含、内联 CSS——可直接
用于邮件发送或归档），并附带纯文本摘要和用于构建它的
原始数据。

## 通过邮件发送报表 {#emailing-reports}

`POST /api/v1/reports/email` 通过你配置的 SMTP 服务器将任意类型的报表
发送给一组收件人——与
[定时任务](automations.md)结合，即可获得定期发送的邮件报表。邮件
投递需要 `EMAIL_ENABLED=true` 和 SMTP 设置
（[配置](../configuration#email-smtp)）。

## API 摘要 {#api-summary}

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/reports/tasks?days=30` | 任务报表 JSON（`title`、`html`、`text_summary`、`data`） |
| `GET /api/v1/reports/usage?days=30` | 用量报表 JSON |
| `GET /api/v1/reports/activity?days=7` | 活动摘要 JSON |
| `GET /api/v1/reports/tasks/html`, `/usage/html` | 原始 HTML 响应 |
| `POST /api/v1/reports/email` | 将报表发送给收件人 |

`days` 接受 1–365。
