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

保留 `DEPLOYMENT_MODE=oss`。自托管部署可包含多个用户和 Entity，并保持权限隔离；
这不改变许可证对第三方托管服务的限制。运行 AI 前先配置当前 Entity 的模型和
BYOK 凭据；Gateway 和语音也不会回退到 Manor 托管密钥。

SMTP、Nango/OAuth、Twilio、媒体供应商和 Sites 托管均为可选依赖。缺配置只阻止
相应操作，不使整个工作区不可用。Sites 需要 `MANOR_SITES_DOMAIN`、根域名及
泛域名 DNS、带 TLS 的反向代理将站点请求转发到 API；留空则禁用托管。
稳定 Flows 在 `MANOR_ENV=prod` 也默认启用，管理员可通过
`FLOWS_AVAILABLE=false` 禁用。实验任务引擎仍需显式启用。

### 可选 Sites 代理

站点使用与认证应用不同的主机。例如设置 `MANOR_SITES_DOMAIN=sites.example.com`，
将泛域名 DNS 指向服务器并取得覆盖 `*.sites.example.com` 的证书。宿主机上的独立
Nginx 代理可使用：

```nginx
server {
    listen 443 ssl;
    server_name *.sites.example.com;
    ssl_certificate /etc/ssl/sites/fullchain.pem;
    ssl_certificate_key /etc/ssl/sites/privkey.pem;
    location / {
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://127.0.0.1:8010/api/v1/site-host/;
    }
}
```

末尾斜杠将 `/` 和 `/__manor/*` 映射到 Sites API。站点主机不应继承认证应用的
`robots.txt` 或全局 noindex 头。自定义域名还需在 Sites 注册，并配置匹配的 DNS、
独立 TLS 证书与代理主机。未知或未发布的站点返回 404。

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
