# Manor DOKS Test / Prod 排障手册

这份手册面向需要观察或初步排查 Manor DOKS 环境的同事。它只涵盖**只读**检查、日志收集和升级路径；日常发布、配置更新和数据修复仍通过 GitHub Actions 与受评审的代码变更完成。

| 环境 | kubectl context | namespace | 发布来源 |
| --- | --- | --- | --- |
| Test | `digitalocean-test` | `manor-digitalocean-test` | `dev` / `K8s Test Release` |
| Prod | `digitalocean-prod` | `manor-digitalocean-prod` | 手动 `K8s Production Dispatch` |

> **边界：** 不要共享 kubeconfig、DigitalOcean token、GitHub Environment Secret、数据库 DSN、Vault unseal key 或 sandbox token。不要在生产执行 `apply`、`delete`、`scale`、`rollout restart`、`patch`、`drain`、`exec` 写入命令，也不要删除 PVC、Job 或 namespace。

## 1. 配置本机 kubectl

### 1.1 获取个人访问权限

1. 向维护者申请加入 Manor 对应的 DigitalOcean project，并申请目标 namespace 的只读 Kubernetes 权限。
2. 使用**自己的** DigitalOcean 账号创建个人 API token。仅用于读取 kubeconfig 时优先使用 Read 权限；不要复用 CI token 或在聊天记录中发送 token。
3. 维护者应只向排障人员授予 `get`、`list`、`watch`、`logs` 等观察权限。拿到 `Forbidden` 时记录命令和错误，交给环境 owner 处理，不要尝试提权。

### 1.2 安装 CLI

macOS：

```bash
brew install kubectl doctl
```

Linux 请按 [kubectl 官方安装说明](https://kubernetes.io/docs/tasks/tools/) 和 [doctl 官方安装说明](https://docs.digitalocean.com/reference/doctl/how-to/install/) 安装。确认版本：

```bash
kubectl version --client
doctl version
```

### 1.3 登录并导入 kubeconfig

`doctl auth init` 会在终端交互输入 token，避免把 token 写入 shell history：

```bash
doctl auth init

# Test
doctl kubernetes cluster kubeconfig save manor-test
kubectl config rename-context "$(kubectl config current-context)" digitalocean-test

# Prod（有获批的生产观察权限时才执行）
doctl kubernetes cluster kubeconfig save manor-prod
kubectl config rename-context "$(kubectl config current-context)" digitalocean-prod
```

首次配置后检查 context，确保没有误连到本地 OrbStack 或其他团队集群：

```bash
kubectl config get-contexts
kubectl --context digitalocean-test get nodes
kubectl --context digitalocean-prod get nodes
```

建议把当前会话固定到一个环境，并始终显式带上 `--context`：

```bash
export CTX=digitalocean-test
export NS=manor-digitalocean-test

# 生产排障时改为：
# export CTX=digitalocean-prod
# export NS=manor-digitalocean-prod

kubectl --context "$CTX" -n "$NS" auth can-i get pods
kubectl --context "$CTX" -n "$NS" auth can-i get pods/log
```

若 `rename-context` 提示目标 context 已存在，不要删除旧 context。先运行 `kubectl config get-contexts`，确认现有名称后由维护者协助清理或直接使用已有 context。

## 2. 第一步：收集现场，不要先改动

每个问题先记录：环境、开始时间（含时区）、访问 URL、用户可见错误、`request_id`、最近一次发布的 workflow run 和镜像 tag。然后执行：

```bash
kubectl --context "$CTX" -n "$NS" get deploy,pods,svc,ingress,jobs,hpa,scaledobject -o wide
kubectl --context "$CTX" -n "$NS" get events --sort-by=.lastTimestamp | tail -100
kubectl --context "$CTX" top nodes
kubectl --context "$CTX" -n "$NS" top pods
```

重点观察：

- `Pending`：查看调度事件，通常是 request、node pool 上限或 PVC 约束。
- `CrashLoopBackOff` / `ImagePullBackOff`：先看该 Pod 的 `describe` 和上一次容器日志。
- `0/1 Ready`：先看 readiness probe、API/Chat 日志和依赖服务状态。
- `HPA/KEDA` 已扩 Pod 但 Pod 仍 Pending：这是 node autoscaler 或资源 request 问题，不要手工删 node。

```bash
kubectl --context "$CTX" -n "$NS" describe pod <pod-name>
kubectl --context "$CTX" -n "$NS" logs <pod-name> --all-containers --previous --tail=200
```

## 3. 按服务查看日志

先用 deployment 日志快速定位；需要连续观察时加 `-f`。多副本服务只显示一个 Pod 时，用下一节的 label 命令汇总。

```bash
# 普通 API、注册、配置、健康检查、WebSocket
kubectl --context "$CTX" -n "$NS" logs deploy/manor-api -c manor-api --tail=200 -f

# Chat SSE、公开 webchat stream、agent stream
kubectl --context "$CTX" -n "$NS" logs deploy/manor-chat -c manor-chat --tail=200 -f

# 常规后台任务与 interactive 执行器
kubectl --context "$CTX" -n "$NS" logs deploy/manor-worker -c manor-worker --tail=200 -f
kubectl --context "$CTX" -n "$NS" logs deploy/manor-worker -c manor-worker-interactive --tail=200 -f

# 重任务
kubectl --context "$CTX" -n "$NS" logs deploy/manor-worker-heavy -c manor-worker-heavy --tail=200 -f

# Beat、迁移和状态依赖
kubectl --context "$CTX" -n "$NS" logs deploy/manor-beat -c manor-beat --tail=200
kubectl --context "$CTX" -n "$NS" logs job/manor-migration --all-containers --tail=300
kubectl --context "$CTX" -n "$NS" logs deploy/vault --tail=200
kubectl --context "$CTX" -n "$NS" logs deploy/minio --tail=200
```

用 API 返回的 `request_id` 跨所有 API Pod 查找，不要把 JWT、Cookie、邮箱 token 或 Authorization header 粘进工单：

```bash
REQUEST_ID='replace-with-request-id'
kubectl --context "$CTX" -n "$NS" logs \
  -l app.kubernetes.io/name=manor-api \
  --all-containers --prefix --since=4h | rg "$REQUEST_ID"
```

同样可以将 `manor-api` 替换为 `manor-chat`、`manor-worker` 或 `manor-worker-heavy` 汇总对应服务的全部副本。

## 4. Chat、队列与 Sandbox

### Chat

先确认 API 和 Chat 都 Ready，再检查 ingress。若问题只发生在流式回复，优先查看 `manor-chat` 日志，而不是 `manor-api`。

```bash
kubectl --context "$CTX" -n "$NS" get deploy manor-api manor-chat
kubectl --context "$CTX" -n "$NS" get ingress
kubectl --context "$CTX" -n ingress-nginx get pods,svc
```

临时从本机验证健康接口时，使用端口转发，不修改 Service：

```bash
kubectl --context "$CTX" -n "$NS" port-forward service/manor-api 18080:8000
# 新开终端：
curl -fsS http://127.0.0.1:18080/health/ready
curl -fsS http://127.0.0.1:18080/health/deep
```

### Celery 与运行时队列

以下命令只读取 worker 的活跃任务；出现长期运行的 `execute_lease`、`runtime.execute_run`、`runtime.resume_run` 或大量同类任务时，把 JSON 输出附到工单。

```bash
kubectl --context "$CTX" -n "$NS" exec deploy/manor-worker -c manor-worker -- \
  celery -A packages.core.celery_app inspect active --timeout=10 --json

kubectl --context "$CTX" -n "$NS" exec deploy/manor-worker-heavy -c manor-worker-heavy -- \
  celery -A packages.core.celery_app inspect active --timeout=10 --json

kubectl --context "$CTX" -n "$NS" get hpa,scaledobject
```

`runtime.sandbox_scheduler`、`runtime.outbox_dispatch` 和 `internal_worker_tick` 是正常的周期任务。不要直接清空 Redis 或执行 `celery purge`；需要取消某个用户运行时，由应用的取消接口或维护者按 root run 定向处理，确保 sandbox 资源同时释放。

### 独立 Sandbox runner

Sandbox runner 不在 DOKS 节点上，而是在同 VPC 的独立 Droplet。拥有该 Droplet SSH 权限的维护者可执行：

```bash
sudo systemctl status manor-sandbox-runner
sudo journalctl -u manor-sandbox-runner -f
sudo docker logs -f manor-sandbox-runner
sudo docker ps -a --filter label=sandbox.service=1
```

不要从本机或 DOKS 节点直接删除 runner 上的容器。先确认对应 runtime run / reservation 是否已取消，再由维护者通过应用清理路径释放；仅在 runner 进程故障且已完成影响评估后处理 orphan container。

## 5. 发布、迁移、网络和扩缩容

### 发布与 migration

发布状态以 GitHub Actions 为准：Test 是 `K8s Test Release`，Prod 是 `K8s Production Dispatch` 后调用的 production release。不要对 DOKS overlay 直接运行 `kubectl apply -k`。

```bash
kubectl --context "$CTX" -n "$NS" get jobs
kubectl --context "$CTX" -n "$NS" logs job/manor-migration --all-containers --tail=300
kubectl --context "$CTX" -n "$NS" rollout status deploy/manor-api --timeout=5m
kubectl --context "$CTX" -n "$NS" rollout history deploy/manor-api
```

数据库 schema 只向前迁移。迁移失败或数据库连接失败时，保存 migration 日志和 workflow 链接，升级给发布负责人；不要删除 Job、PVC 或尝试用旧镜像回滚 schema。

### NetworkPolicy / 邮件外发

```bash
kubectl --context "$CTX" -n "$NS" get networkpolicy
kubectl --context "$CTX" -n "$NS" logs deploy/smtp-egress-proxy --tail=200
kubectl --context "$CTX" -n "$NS" get endpointslice -l kubernetes.io/service-name=smtp-egress-proxy
```

注册邮件或外部 API 异常时，优先从 API `request_id`、`manor-api`、`smtp-egress-proxy` 日志与 NetworkPolicy 事件判断，不要打印或修改 SMTP / OAuth 密钥。

### Pod 与 node autoscaling

```bash
kubectl --context "$CTX" -n "$NS" get hpa,scaledobject
kubectl --context "$CTX" -n "$NS" describe hpa manor-api
kubectl --context "$CTX" get nodes -o wide
kubectl --context "$CTX" top nodes
kubectl --context "$CTX" -n "$NS" get pods --field-selector=status.phase=Pending -o wide
kubectl --context "$CTX" -n "$NS" get events --sort-by=.lastTimestamp | tail -100
```

HPA/KEDA 先扩 Pod；资源不足时 DOKS Cluster Autoscaler 才扩 node。缩容通常需要 15-30 分钟。不要因为看到一台新增或待删 Droplet 就手动 `cordon`、`drain` 或删除 node；将 Pending Pod 的 `describe`、HPA/KEDA 和节点指标交给环境 owner 判断。

## 6. 交给 AI 协助排查

可以让 AI 协助分析**已脱敏**的日志和执行只读命令。每次先给足环境信息，再限制权限边界：

```text
你是 Manor DOKS 的只读排障助手。
环境：Test / Prod
kubectl context：digitalocean-test / digitalocean-prod
namespace：manor-digitalocean-test / manor-digitalocean-prod
问题开始时间：<含时区>
用户可见现象：<错误、URL、SSE 断开等>
request_id：<可选>

只允许执行 kubectl get/describe/logs/top/auth can-i、只读 celery inspect、
以及 curl 本机 port-forward 健康检查。禁止 apply/delete/scale/patch/restart/
drain、禁止修改数据库或 Redis、禁止读取或输出 Secret、ConfigMap、环境变量、
kubeconfig、token、DSN、Vault unseal key。先给出证据和最小根因假设，
再提出需要环境 owner 执行的修复建议。
```

发给 AI 或工单的内容必须脱敏：保留时间、服务名、Pod 名、request id、状态码、错误类型和 stack trace；删除邮箱、用户内容、Cookie、Authorization、JWT、数据库 URL、密码、密钥和 Vault material。

## 7. 升级给环境 owner 时附带的证据

1. 环境、发生时间、影响范围和 GitHub Actions run URL。
2. `kubectl get deploy,pods,jobs,hpa,scaledobject -o wide` 输出。
3. 最新 100 条 events。
4. 对应服务日志、`request_id` 检索结果或 worker active JSON，全部脱敏。
5. 已执行的只读检查，以及明确说明“没有执行任何写入或资源删除操作”。

关联文档：[K8s 部署与测试指南](K8S_DEPLOYMENT_ZH.md)、[DOKS Test 部署](../docs/DIGITALOCEAN_DOKS_TEST_DEPLOYMENT_ZH.md)、[DOKS 生产部署](../docs/DIGITALOCEAN_DOKS_PRODUCTION_DEPLOYMENT_ZH.md)。
