---
title: 存储
---

# 存储

Manor AI 同时使用 PostgreSQL、Redis、MinIO 和 JuiceFS。每一项承载不同
类型的状态；理解这种分工，备份、扩容和调试才有章可循。

## PostgreSQL {#postgresql}

PostgreSQL（16，带 **pgvector**）存储所有关系型应用数据：用户、实体、
工作区、任务、目标、文档元数据、对话、工作流定义与运行记录，以及为语义
搜索保存分块嵌入（embedding）的 `document_chunks` 表。

- 栈内置 `pgvector/pgvector:pg16`；没有 pgvector 扩展的原生 PostgreSQL
  无法工作。
- 每次升级前先备份——参见
  [备份与恢复](backup-restore.md)。
- 如果调高 `API_WORKERS` 或连接池大小，请留意连接数；参见
  [配置](../configuration#database)中的容量说明。

## Redis {#redis}

一个 Redis 实例在不同的逻辑数据库上承担四种独立角色：

| 数据库 | 角色 |
| --- | --- |
| `/0` | 应用缓存、限流、在线状态（`REDIS_URL`） |
| `/1` | **JuiceFS 元数据**——持久数据，必须备份 |
| `/2` | Celery broker |
| `/3` | Celery 结果后端 |

由于 `/1` 承载文件系统元数据，这个 Redis *不是*可随意丢弃的缓存。
`REDIS_MAXMEMORY` 默认为 `0`（不限制）并配合 `noeviction`——如果你要
限制内存，绝不能使用可能淘汰 JuiceFS 键的驱逐策略；应先把 JuiceFS
元数据迁移到独立的 Redis。

## MinIO {#minio}

MinIO 提供 S3 兼容的对象存储，用于上传的文件、文档资产、生成的媒体产物
以及 JuiceFS 数据块（存储桶通过 `MINIO_BUCKET` / `JUICEFS_BUCKET` 配置）。

## JuiceFS {#juicefs}

JuiceFS 为 Manor AI 提供每个实体一个的 POSIX 文件系统——也就是 Agent
文件工具读写的界面——由 Redis 元数据（`/1`）和 MinIO 对象块支撑。
`juicefs-init` 一次性服务在栈启动时完成格式化和挂载；API 和 worker
容器通过 FUSE 将其挂载在 `MANOR_FS_ROOT`（默认 `/mnt/manor`）。

由此带来的影响：

- 文件无法直接从 MinIO 存储桶读取——数据块只有配合 JuiceFS 的元数据
  才有意义。
- 元数据（Redis `/1`）和数据块（MinIO）必须成对备份。
- worker 停止时有 30 秒宽限期，以便 FUSE 层把写缓存刷新到 MinIO；
  避免使用 SIGKILL。

## 嵌入（Ollama） {#embeddings-ollama}

文档 RAG 使用本地 Ollama 服务和 `mxbai-embed-large` 模型（1024 维），
首次启动时由 `ollama-init` 预加载到 `ollama_data` 卷。首次启动需要下载
模型，请预留额外时间。缺少该模型时，嵌入（embedding）调用会失败，搜索
会降级为纯文本匹配。

## 生产环境注意事项 {#production-notes}

- 为每个有状态服务使用持久卷（`pg_data`、`redis_data`、
  `minio_data`、`ollama_data`）。
- 监控磁盘用量——生成的媒体和文档上传会在 MinIO 中不断累积。
- 将 PostgreSQL、MinIO 和 Redis `/1` 一起备份；不要让文件系统快照与
  数据库快照产生时间上的漂移。
