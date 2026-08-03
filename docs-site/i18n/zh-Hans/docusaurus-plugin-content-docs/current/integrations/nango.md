---
title: Nango
---

# Nango

Nango 是一个可选的自托管 OAuth 与 API 连接器服务。Manor AI 可以借助它
连接众多 SaaS 提供商，而无需从零构建每一个 OAuth 流程。

## 启动 Nango {#starting-nango}

Compose 文件在 `nango` profile 下包含了 Nango 相关服务。

```bash
docker compose --profile nango up -d nango-postgres nango-server
```

打开 Nango 界面，创建或复制密钥，并将它们填入你的 Manor AI 配置。

## 何时使用 Nango {#when-to-use-nango}

在以下情况使用 Nango：

- 提供商要求使用 OAuth。
- 你希望在多个工作区之间复用同一个连接器。
- 你倾向于使用自托管的集成中枢。

当提供商的接入流程比较简单时，直接使用 API 密钥或 webhook 即可。
