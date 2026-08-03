---
sidebar_position: 2
title: 快速开始
---

# 快速开始

从全新克隆到跑起一个 Manor AI 工作区的最短路径。镜像在本地就绪后,预计
5-10 分钟。

<div className="ma-doc-actions">
  <a className="ma-button ma-button--primary" href="#5-分钟路径">按路径操作</a>
  <a className="ma-button ma-button--secondary" href="configuration">查看配置</a>
</div>

完成本指南后,你将拥有:

- 打开在 `http://localhost:18080` 的 Manor AI Web 应用。
- 已登录的本地演示账号。
- 运行中的 API、worker、PostgreSQL、Redis、MinIO 和沙箱服务。
- 已配置的模型提供商密钥,Agent 可以回答问题。

## 开始之前

安装:

| 依赖 | 版本要求 | 用途 |
| --- | --- | --- |
| Docker Compose | v2 | 启动自托管服务栈 |
| Python | 3.11+ | 本地脚本、检查与开发工具 |
| Node.js | 20+ | 构建 Web 应用与文档 |
| Git | 最新稳定版 | 克隆与更新仓库 |

> 共享部署前请先修改密钥。默认 `.env.example` 仅供本地评估;可从公网访问
> 的部署必须为 `JWT_SECRET_KEY`、MinIO 凭据和所有 OAuth client secret
> 设置强随机值。

## 5 分钟路径

<div className="ma-path-table">

| 步骤 | 命令或页面 | 应该看到什么 |
| --- | --- | --- |
| 1 | 克隆仓库 | 本地的 `manor-ai` 检出 |
| 2 | 启动 Docker Compose | 核心容器变为 healthy |
| 3 | 打开 Web 应用 | `localhost:18080` 的登录页 |
| 4 | 登录 | 演示账号进入工作区界面 |
| 5 | 添加模型密钥 | 聊天或 Agent 能回答简单提示词 |

</div>

### 1. 克隆

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
```

### 2. 启动服务栈

```bash
docker compose up --build -d
```

### 3. 打开 Manor AI

打开:

```text
http://localhost:18080
```

自托管模式默认预置一个本地演示账号:

```text
demo@manor.local / manor-demo
```

### 4. 配置模型提供商

打开设置,为你想使用的模型路径添加提供商密钥。自托管模式下 Manor AI 采用
BYOK(自带密钥);模型凭据只保存在你自己的部署中。

### 5. 冒烟测试

打开聊天或 Agents 页,发一条简短提示词。如果界面正常但模型不回答,先检查
提供商配置。

## 验证服务

```bash
docker compose ps
docker compose logs api --tail=100
docker compose logs worker --tail=100
docker compose logs web --tail=100
```

预期的核心服务状态:

- PostgreSQL 和 Redis 处于 healthy。
- API 在配置的端口上可达。
- Web 应用正常提供 React 工作区界面。
- worker 正在运行后台任务。
- MinIO 对象存储可用。
- 若启用了 Agent 代码执行,沙箱服务可用。

## 常见本地问题

| 现象 | 排查方向 |
| --- | --- |
| 登录页打不开 | `docker compose ps`,然后 `docker compose logs web --tail=100` |
| API 返回 500 | `docker compose logs api --tail=100` 与数据库健康状态 |
| Agent 不回答 | 模型提供商密钥、模型名称、提供商网络可达性 |
| 文件或文档报错 | MinIO 凭据与存储配置 |
| 沙箱工具失败 | Docker socket 权限与 `SANDBOX_SERVICE_URL` |
| 端口被占用 | 修改 `docker-compose.yml` 中的宿主端口映射,或停掉冲突服务 |

## 下一步

- 向用户开放前,先阅读[配置](configuration.md)。
- 存放重要数据前,先阅读[备份与恢复](operations/backup-restore.md)。
- 允许 Agent 执行敏感操作前,先阅读[人工审批治理](concepts/hitl-governance.md)。
