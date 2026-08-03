---
title: 升级与发布
---

# 升级与发布

把 Manor AI 的升级当作带数据库迁移的应用发布来对待：先备份、再应用、
然后验证，并始终保留回滚路径。

## 推荐流程 {#recommended-flow}

1. 阅读你要跨越的版本区间的发布说明 / `CHANGELOG.md`。
2. 将 PostgreSQL、MinIO 和 Redis `/1` 一起备份
   （[备份与恢复](backup-restore.md)）。
3. 拉取新的源码。
4. 重新构建镜像并重启栈——迁移会在 API 启动时应用。
5. 观察 API 和 worker 日志直到健康。

```bash
git pull
docker compose up --build -d
docker compose logs api --tail=200
docker compose logs worker --tail=200
```

6. 验证：登录、打开一个工作区、跑一次快速的 Agent 对话，并检查
   `GET /health/deep` 返回健康状态。

`api`、`worker` 和 `worker-work` 服务共享同一个镜像；Compose 只构建
一次并重建全部三个容器。两个 worker 都必须恢复运行——调度在
`worker` 中，长计划步骤在 `worker-work` 中。

## 回滚 {#rollback}

回滚的难度取决于迁移是否已经应用：

- **没有 schema 变更**：检出上一个版本并执行
  `docker compose up --build -d`。
- **schema 已变更**：恢复升级前的数据库（和对象存储）备份，然后启动
  上一个版本。不要用旧版本应用去跑新版本的 schema。

务必保留紧邻升级前的那份备份，以便数据库和对象存储能够一起恢复。

## 版本标签 {#version-tags}

公开发布从 Git 标签发出。发布工作流会为匹配 `v*` 的标签创建 GitHub
发布说明。在标签之间跟踪 `main` 也可行，但要把它当作滚动发布：升级前
先阅读最近的提交信息，留意包含迁移的变更。
