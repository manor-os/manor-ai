---
sidebar_position: 3
title: 安装
---

# 安装

Manor AI 支持两种常见的自托管方式:

- Docker Compose:单机、接近生产的部署。
- 本地开发:Python、Node.js、PostgreSQL、Redis、MinIO。

## Docker Compose

推荐的首次安装路径。

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
docker compose up --build -d
```

这会启动 API、Web 前端、worker、PostgreSQL、Redis、MinIO、沙箱及配套服务。

## 本地开发

需要改代码时使用这条路径。

```bash
cp .env.example .env
pip install ".[dev]"
cd apps/web
npm ci
cd ../..
./scripts/dev.sh infra
./scripts/dev.sh init
```

在两个终端里分别运行 API 和 Web 应用:

```bash
./scripts/dev.sh api
./scripts/dev.sh web
```

可选的 worker:

```bash
./scripts/dev.sh worker
```

## 数据库初始化

`./scripts/dev.sh init` 会执行迁移和种子数据逻辑。Docker 部署通过容器
entrypoint 走同一套初始化路径。

## 更新已有安装

源码安装:

```bash
git pull
pip install ".[dev]"
cd apps/web && npm ci && cd ../..
docker compose up --build -d
```

对共享环境应用更新前,请先阅读发布说明。
