---
sidebar_position: 10
title: 日历与预约链接
---

# 日历与预约链接

Manor 内置了个人日历设置和公开的、Calendly 风格的预约
链接，让日程安排可以流经运行你任务的同一套系统。

## 日历设置 {#calendar-settings}

在你的账户设置下可以配置：

- **工作时间**——按星期几设置的时间窗口。
- **预约默认值**——会议时长（5–480 分钟）、会前会后
  缓冲时间、最短提前通知时间，以及他人最远可提前多久预约（1–365 天）。
- **外部日历**——通过 OAuth 连接 Google Calendar 或 Microsoft Calendar，
  将外部日程拉入可用时段和每日议程
  （`GET /api/v1/calendar-settings/events`、`GET /api/v1/calendar-settings/day`）。

## 预约链接 {#booking-links}

每个预约链接都有一个 slug、一个名称、一个时长和一个地点类型
（`phone`、`video`、`in_person`、`custom` 或无）。公开页面
`/book/{slug}` 会根据你的工作时间、缓冲时间、
最短提前通知时间和预约窗口计算可用时段——预约者
无需任何身份认证。

预约确认后会在 Manor 中创建一个**任务**并通知你，因此预约
会与其他工作一起出现在同一个看板、议程和 Agent 上下文
中。

## API 摘要 {#api-summary}

| 端点 | 用途 |
| --- | --- |
| `GET` / `PUT /api/v1/calendar-settings` | 读取 / 更新工作时间与默认值 |
| `POST /api/v1/calendar-settings/booking-links`, `PUT/DELETE .../booking-links/{id}` | 管理链接 |
| `GET /api/v1/calendar-settings/public/booking-links/{slug}` | 公开：链接信息 + 可用时段 |
| `POST .../public/booking-links/{slug}/book` | 公开：预约一个时段 |
| `GET /api/v1/calendar-settings/events` | 外部日历日程（Google / Microsoft） |
| `GET /api/v1/calendar-settings/day` | 每日议程 |
