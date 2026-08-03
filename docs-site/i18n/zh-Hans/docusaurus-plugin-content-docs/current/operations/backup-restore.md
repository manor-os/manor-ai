---
title: 备份与恢复
---

# 备份与恢复

备份应覆盖结构化数据、对象存储和配置。三者要一起备份：文档、知识和
实体文件系统横跨 PostgreSQL、MinIO *以及* Redis 中保存的 JuiceFS
元数据，只有各部分来自同一时间点，恢复才是一致的。

## 需要备份什么 {#what-to-back-up}

| 内容 | 所在位置 | 原因 |
| --- | --- | --- |
| PostgreSQL | `pg_data` 卷 | 所有关系型数据：用户、工作区、任务、目标、运行记录、消息。 |
| MinIO | `minio_data` 卷 | 上传的文件、生成的产物，以及 JuiceFS 数据块。 |
| Redis 数据库 `/1` | `redis_data` 卷 | JuiceFS **元数据**。没有它，MinIO 中的 JuiceFS 数据块将无法读取。 |
| `.env` | 宿主机 | 密钥、服务商凭据和部署配置。 |
| OAuth 应用注册信息 | 外部服务商 | 重新连接集成所需的 client id/secret 和回调 URL。 |

## 内置导出 API {#built-in-export-api}

API 内置了用于逻辑备份的租户导出接口：

```text
GET  /api/v1/backup/summary          # what an export would contain
GET  /api/v1/backup/export           # export the tenant's data
POST /api/v1/backup/export/download  # download an export archive
```

这是数据级导出（适合迁移和异地副本），不能替代对整个部署的卷级备份。

## PostgreSQL {#postgresql}

导出：

```bash
docker compose exec postgres pg_dump -U manor manor > manor.sql
```

恢复到兼容的 PostgreSQL 版本：

```bash
cat manor.sql | docker compose exec -T postgres psql -U manor manor
```

对于大型数据库，优先使用自定义格式（`pg_dump -Fc`）配合
`pg_restore`，并按计划定期执行导出（单机部署用宿主机上的 cron 即可）。

## MinIO 与 JuiceFS {#minio-and-juicefs}

使用 MinIO 客户端工具（`mc mirror`）或对 `minio_data` 做卷快照。
两条规则：

- 对象备份要与数据库备份保持协同——文档记录存在但文件 blob 缺失
  （或反过来）都意味着恢复是坏的。
- JuiceFS 数据只有配合其元数据才可用。在对 `minio_data` 做快照的
  同时对 `redis_data`（至少是 Redis 数据库 `/1`）做快照。只恢复
  MinIO 而没有匹配的 Redis 元数据，即使数据块还在，实体文件系统也
  会是空的。

如果需要严格一致的文件系统镜像，在做快照前先停止或静默 worker；
FUSE 挂载在正常停止时会刷新其缓存。

## 恢复演练 {#restore-practice}

不要等到事故发生才测试恢复。定期恢复到一个独立环境并确认：

1. 登录正常，用户/工作区都在。
2. 文档可以打开，知识搜索返回结果（检验 PostgreSQL、MinIO *以及*
   JuiceFS 元数据的对齐）。
3. 一次 Agent 运行能够完成。
4. 集成能重新连接（或干净地重新授权）。

每次升级前都保留至少一份紧邻升级的备份——参见
[升级与发布](upgrade-release.md)。
