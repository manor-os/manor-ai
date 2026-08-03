---
sidebar_position: 13
title: 安全
---

# 安全

本页汇总面向自托管部署的运维安全指引。漏洞报告请参见仓库根目录的
`SECURITY.md`。

## 投入生产之前 {#before-production-use}

- 替换所有默认密钥：`JWT_SECRET_KEY`、PostgreSQL 密码、
  `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`（以及对应的 `JUICEFS_*`
  凭据）和 Vault token。
- 为 `APP_URL` 和 `PUBLIC_BASE_URL` 使用 HTTPS。OAuth 回调和大多数
  渠道 webhook 都要求 HTTPS。
- 将 PostgreSQL、Redis、MinIO、Vault、Ollama 和沙箱服务限制在私有
  Docker 网络内。只有 `web` 服务（以及你使用的反向代理，如果有）
  应能从外部访问。
- 启用限流（`RATE_LIMIT_ENABLED`、`CHAT_RATE_LIMIT_ENABLED`，运行多个
  API worker 时再加上 `REDIS_RATE_LIMIT_ENABLED`）。
- 配置备份并测试恢复
  （[备份与恢复](operations/backup-restore.md)）。
- 为敏感的 Agent 操作要求[人工审批（HITL）](concepts/hitl-governance.md)，
  并把 Agent 工具作用域限制到每个 Agent 实际需要的范围。
- 定期轮换服务商凭据。
- 在对外暴露部署之前，删除或修改预置的演示账号。

## 机密 {#secrets}

绝不要提交 `.env`、服务商密钥、OAuth 密钥、webhook 密钥、数据库密码
或生产日志。

静态存储时，集成凭据通过 Vault transit 后端加密（`VAULT_TOKEN`、
`VAULT_TRANSIT_KEY`）。Compose 栈自带一个本地开发用 Vault；生产环境
要么保护好其存储，要么把 Manor 指向外部 Vault。

模型服务商密钥采用 BYOK，通过“设置”页面录入——它们（加密后）保存
在你部署的数据库中，绝不进入源码或镜像。

## 会话与账号安全 {#session-and-account-security}

- 会话是用 `JWT_SECRET_KEY` 签名的 JWT；过期时间默认 24 小时
  （`JWT_EXPIRE_MINUTES`）。
- 用户可以在**设置 → 安全**中启用双因素认证（TOTP）并查看活跃会话。
- 用于程序化访问的 API 密钥在**设置 → API 密钥**中管理；将其作用域
  收窄，并在人员变动时轮换。

## 公开端点 {#public-endpoints}

少数路由有意不做认证；对外暴露部署时要了解它们：

| 端点 | 用途 | 保护措施 |
| --- | --- | --- |
| `POST /api/v1/workflows/webhook/{token}` | 工作流 webhook 触发器 | token 就是该绑定的共享密钥；通过重建绑定来轮换。 |
| `GET/POST /api/v1/channels/...` webhooks | 入站渠道消息 | 服务商签名校验（Meta HMAC、Discord Ed25519、Twilio 签名）或服务商特定的 challenge 流程。 |
| `GET /api/v1/calendar-settings/public/booking-links/...` | 公开预约页面 | 只读的可用时段；预约会创建任务，不暴露数据。 |
| 共享文档 / 公开聊天 token 链接 | 显式分享 | 能力 token；把链接当作机密对待。 |

## 沙箱与 Agent 执行 {#sandbox-and-agent-execution}

Agent 代码和 shell 执行运行在隔离的
[沙箱服务](operations/sandbox.md)中，带有内存、CPU、PID 和超时限制
以及只读根文件系统。保持沙箱访问仅限内部，如果不需要 shell 工具就
关闭 `SHELL_SANDBOX_ENABLED`，并为具备执行能力的 Agent 配套显式的
治理规则。
