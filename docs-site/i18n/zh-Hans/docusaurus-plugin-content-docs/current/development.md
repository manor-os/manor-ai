---
sidebar_position: 11
title: 开发
---

# 开发

## 环境搭建 {#setup}

```bash
cp .env.example .env
pip install ".[dev]"
cd apps/web
npm ci
cd ../..
./scripts/dev.sh infra
./scripts/dev.sh init
```

运行各项服务：

```bash
./scripts/dev.sh api
./scripts/dev.sh web
./scripts/dev.sh worker
```

## 测试 {#tests}

```bash
make test
make test-regression
make test-e2e
make test-all
```

默认测试目标会排除标记为慢速、手动、网络、Docker 和云端的测试。

## 前端 {#frontend}

```bash
cd apps/web
npm ci
npm run dev
npm run build
```

## 代码风格 {#style}

Python 使用 Ruff 检查。TypeScript 使用严格模式。

```bash
make lint
make format
```

## 贡献 {#contributions}

参见仓库根目录的 `CONTRIBUTING.md`。保持变更聚焦，为行为变更添加测试，
并在安装步骤或面向用户的行为发生变化时更新文档。
