---
sidebar_position: 5
title: Docker Compose
---

# Docker Compose

Compose 技术栈是在单台主机上运行一个接近生产环境的 Manor AI 实例
的最快方式。

## 核心服务 {#core-services}

以下服务在直接执行 `docker compose up` 时启动：

| 服务 | 角色 |
| --- | --- |
| `web` | 由 Nginx 提供服务的 React 前端。监听 `18080`，并将 `/api` 代理到 API 容器。 |
| `api` | FastAPI 应用。 |
| `worker` | Celery 控制平面 worker，**内含 beat**：调度任务、派发租约、运行运维监控。消费默认的 `celery` 队列。 |
| `worker-work` | Celery 工作队列 worker：在 `work` 队列上运行耗时较长的 Agent 计划步骤，使长步骤不会阻塞调度。 |
| `postgres` | 带 pgvector 的 PostgreSQL 16（`pgvector/pgvector:pg16`）。 |
| `redis` | 缓存、Celery broker、限流后端与 JuiceFS 元数据。 |
| `minio` | 兼容 S3 的对象存储。 |
| `juicefs-init` | 一次性任务：格式化并挂载实体文件系统存储，然后退出。 |
| `ollama` + `ollama-init` | 本地 embedding 运行时。`ollama-init` 在首次启动时预加载 `mxbai-embed-large`，让文档 RAG 开箱即用；没有该模型时，embedding 调用会失败，搜索会退化为纯文本搜索。 |
| `sandbox` | 隔离的代码执行服务，管理按运行分配的 Docker 容器。 |
| `sandbox-skill-image` | 构建沙箱子容器使用的基础镜像，因此 `docker compose up --build -d` 无需单独的预构建步骤。 |
| `vault` | 用于开发风格部署的本地密钥加密助手（Vault transit）。 |

### 双 worker 拆分 {#the-two-worker-split}

`worker` 和 `worker-work` 共享同一个镜像和配置；仅队列
和并发数不同。控制平面 worker（`-Q celery`，并发数
`WORKER_CONTROL_CONCURRENCY`，默认 2）运行 beat、租约派发和
监控。工作队列 worker（`-Q work`，并发数 `WORKER_WORK_CONCURRENCY`，
默认 4）运行面向用户的计划步骤，不含 beat，也没有 Docker socket
访问权限。二者必须一起部署：beat 只负责*入队*工作；在
`worker-work` 启动之前，没有任何进程消费 `work` 队列。

### Redis 数据库分配 {#redis-database-allocation}

一个 Redis 实例通过独立的逻辑数据库承担四种角色：

| DB | 用途 |
| --- | --- |
| `/0` | 应用缓存、限流、在线状态（`REDIS_URL`） |
| `/1` | JuiceFS 元数据（`JUICEFS_META_URL`） |
| `/2` | Celery broker |
| `/3` | Celery 结果后端 |

如果你将 Manor 指向外部 Redis，请保持这些数据库彼此分离。

## 可选 profile {#optional-profiles}

使用 `--profile <name>` 启用额外服务：

| Profile | 服务 | 增加的能力 |
| --- | --- | --- |
| `nango` | `nango-server`, `nango-postgres` | 自托管的 [Nango](integrations/nango)，提供 200+ SaaS OAuth 集成。 |
| `wechat` | `wechat-runner` | 微信渠道桥接。启动后打开 `http://localhost:8801/qr.png` 并用微信扫码配对。 |
| `observability` | `jaeger` | 用于 OpenTelemetry 追踪的 Jaeger all-in-one（通过 `OTEL_ENABLED=true` 启用）。 |
| `jimeng` | `jimeng-api` | 可选的即梦（Jimeng）图像生成网关。 |

```bash
docker compose --profile nango up -d
```

## 常用命令 {#common-commands}

```bash
docker compose up --build -d
docker compose ps
docker compose logs api --tail=100
docker compose logs worker --tail=100
docker compose down
```

## 重建单个服务 {#rebuilding-one-service}

```bash
docker compose build api
docker compose up -d api worker worker-work
```

`api`、`worker` 和 `worker-work` 服务共享 `manor-api` 镜像——
构建一次，重启三个服务。

## 健康检查 {#health-checks}

先用 `docker compose ps` 查看；每个长期运行的服务都定义了
健康检查，依赖服务会等待 `service_healthy`。如果某个服务
不健康，请检查其日志及其依赖项：

```bash
docker compose logs postgres --tail=100
docker compose logs redis --tail=100
docker compose logs api --tail=200
```

API 还暴露了 `GET /health`、`GET /health/ready` 和
`GET /health/deep`（检查数据库、Redis 和存储）。控制平面
worker 会在 Compose 网络内部探测 `http://api:8000/health/deep`，作为
运维监控的一部分。

启动顺序通过依赖关系强制执行：`juicefs-init` 和
`ollama-init` 必须在 API 和 worker 启动前完成，因此首次
启动较慢（镜像拉取、模型下载）是正常现象。

## 持久化数据 {#persistent-data}

命名卷保存所有状态：

| 卷 | 内容 |
| --- | --- |
| `pg_data` | PostgreSQL 数据目录 |
| `redis_data` | Redis 持久化数据 |
| `minio_data` | 对象存储（上传文件、JuiceFS 数据块） |
| `ollama_data` | 已下载的 embedding 模型 |
| `wechat_runner_data` | 微信会话状态（profile `wechat`） |
| `nango_pg_data` | Nango 自己的数据库（profile `nango`） |

除非你打算重置部署，否则不要删除卷。升级前请
先创建备份——参见[备份与恢复](operations/backup-restore.md)。

## 干净地停止 {#stopping-cleanly}

worker 通过 FUSE 挂载 JuiceFS，停止时有 30 秒的宽限期，
用于排空进行中的任务并将缓存刷写到 MinIO。请优先使用
`docker compose stop` / `docker compose down`，而不是直接杀掉容器；在
刷写过程中收到 SIGKILL 可能会留下卡死的 FUSE 挂载，需要手动清理。
