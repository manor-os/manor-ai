# Manor K8s 部署与测试指南

本目录保存 Manor 的 Kubernetes 清单。本指南说明如何启动本地 K8s 测试环境、单节点服务器测试环境，以及 DigitalOcean DOKS 环境。

不要将 local 或 single-node 测试路径用于生产。它们使用集群内 PostgreSQL、Redis、MinIO 和本地镜像；生产需要外部数据库、持久化存储、备份、真实 TLS 和不可变镜像。

## 1. 选择部署路径

| 场景 | 入口脚本 | 默认 context / namespace | 适用范围 |
| --- | --- | --- | --- |
| 本机 OrbStack | scripts/k8s_local_apply.sh | orbstack / manor-local | 开发、功能验证、Chrome/agent 调试 |
| 通用单节点测试服务器 | scripts/k8s_single_node_apply.sh | manor-test / manor-test | 有公网入口的临时测试节点 |
| DigitalOcean DOKS test/prod | scripts/k8s_digitalocean_apply.sh | 环境决定 | 托管集群发布 |
| 已准备好的生产 overlay | scripts/k8s_production_apply.sh | manor-prod | 受保护的生产发布流程 |

所有脚本都会校验当前 kubectl context。执行前先确认目标，避免在错误集群应用：

~~~bash
kubectl config current-context
kubectl get nodes
~~~

## 2. 本地 K8s 测试

### 前置条件

1. 启动 OrbStack Kubernetes，并确认 context 为 orbstack。
2. 安装 Docker、kubectl 和本仓库依赖。
3. 可选地在仓库根目录准备 .env。脚本会将允许的应用配置和密钥分离为 namespace 内的 ConfigMap/Secret；不要提交该文件。

正常启动会构建 API、Web、sandbox service 和 sandbox runtime 镜像，然后按有状态依赖、migration Job、API/Chat/Worker/Web 的顺序部署：

~~~bash
EXPECTED_CONTEXT=orbstack \
NAMESPACE=manor-local \
K8S_APP_ENV_FILE=.env \
scripts/k8s_local_apply.sh
~~~

K8S_APP_ENV_FILE 可省略：存在 .env 时会自动使用。仅当镜像已经构建好时才使用 BUILD_IMAGES=false；否则 sandbox runtime 镜像缺失会使 smoke 失败。

使用 kind、k3s 或 microk8s 时，设置正确的 context 和镜像导入模式。例如：

~~~bash
EXPECTED_CONTEXT=kind-manor \
K8S_LOCAL_IMAGE_IMPORT_MODE=auto \
scripts/k8s_local_apply.sh
~~~

### 访问与验证

local 服务默认是 ClusterIP，不会自动暴露宿主机端口。为浏览器保持一个端口转发：

~~~bash
kubectl -n manor-local port-forward service/manor-web 18082:80
~~~

访问 <http://127.0.0.1:18082>。需要直接调试服务时可另开终端：

~~~bash
kubectl -n manor-local port-forward service/manor-api 18080:8000
kubectl -n manor-local port-forward service/manor-chat 18081:8000
kubectl -n manor-local port-forward service/manor-sandbox 18110:8000
~~~

运行完整本地 smoke。该脚本会临时创建并在退出时清理端口转发和 smoke Pod：

~~~bash
EXPECTED_CONTEXT=orbstack \
NAMESPACE=manor-local \
scripts/k8s_local_smoke.sh
~~~

检查运行状态：

~~~bash
kubectl -n manor-local get deployments,pods,jobs,pvc
kubectl -n manor-local logs job/manor-migration --tail=200
~~~

### 从本机 Docker Compose 迁移数据

先完成一次 local K8s 启动，再使用以下命令将同机 Compose 的 PostgreSQL、应用 MinIO bucket 和逻辑 /mnt/manor 覆盖迁入 manor-local：

~~~bash
EXPECTED_CONTEXT=orbstack \
NAMESPACE=manor-local \
scripts/compose_to_local_k8s.sh \
  --confirm migrate-compose-to-manor-local
~~~

脚本会停止目标应用、备份目标数据、校验 PostgreSQL 表数、MinIO 对象和文件条目，再重启本地栈。成功后自动删除临时迁移目录；失败时保留目录并让目标应用维持停止，便于检查。Redis、JuiceFS metadata 和历史 recovery bucket 不迁移。

local overlay 为 PostgreSQL、MinIO 和 /mnt/manor 分别配置 PVC。不要删除 manor-local namespace 或这些 PVC，除非明确要销毁本地数据。

## 3. 单节点服务器测试

单节点路径适用于临时的公网测试服务器，要求目标节点已安装 Docker、kubectl 和单节点 Kubernetes 发行版，并已配置 ingress controller、DNS 和可选 TLS Secret。

通用单节点部署：

~~~bash
EXPECTED_CONTEXT=manor-test \
NAMESPACE=manor-test \
HOST=test.example.com \
K8S_APP_ENV_FILE=/srv/manor/.env \
K8S_LOCAL_IMAGE_IMPORT_MODE=auto \
scripts/k8s_single_node_apply.sh
~~~

优先使用 K8S_APP_ENV_FILE；K8S_LOCAL_ENV_FILE 仍可作为兼容别名。若 K3s runtime 不能直接使用 Docker 镜像，K8S_LOCAL_IMAGE_IMPORT_MODE=auto 会选择可用的导入方式；也可以显式设置为 kind、k3s 或 microk8s。

部署完成后：

~~~bash
kubectl --context manor-test -n manor-test get deployments,pods,jobs
EXPECTED_CONTEXT=manor-test \
NAMESPACE=manor-test \
scripts/k8s_local_smoke.sh
~~~

将 command 中的 context 和 namespace 替换为实际值。单节点前置条件和 TLS 细节见 [docs/K8S_SINGLE_NODE_TEST_SERVER_ZH.md](../docs/K8S_SINGLE_NODE_TEST_SERVER_ZH.md)。

## 4. DigitalOcean DOKS

DOKS 入口会构建并推送镜像、同步 Secret、按 migration 顺序发布 split services。不要在 shell history、仓库或 CI 日志中保存数据库密码、registry token 和 Vault token。

### Test 环境

从示例复制受保护的运行时和备份配置，填写真实值并保存在仓库外：

~~~bash
cp deploy/k8s/env/test-runtime.env.example /secure/manor/test-runtime.env
~~~

执行 test 发布：

~~~bash
DIGITALOCEAN_DEPLOY_ENV=test \
DOKS_TEST_PROFILE=compact \
EXPECTED_CONTEXT=digitalocean-test \
NAMESPACE=manor-digitalocean-test \
HOST=test.example.com \
IMAGE_REGISTRY=ghcr.io/manor-os \
REGISTRY_USERNAME=replace-me \
REGISTRY_PASSWORD=replace-me \
K8S_APP_ENV_FILE=/secure/manor/test-app.env \
K8S_DIGITALOCEAN_RUNTIME_SECRET_FILE=/secure/manor/test-runtime.env \
REQUIRE_BACKUP_SECRET_FILE=false \
SANDBOX_SERVICE_URL=http://10.0.0.10:8000 \
scripts/k8s_digitalocean_apply.sh
~~~

DOKS_TEST_PROFILE 可取 compact 或 rehearsal。真实的 HOST、私网 sandbox runner 地址、registry 凭据和 Secret 文件均为必填发布输入；示例中的值不可直接使用。

### Production 环境

生产使用真实 DNS、HTTPS、外部数据库、外部 sandbox runner 和不可变镜像 tag/digest。当前可与 Test 一样由 Cloudflare `Full` 使用 ingress 默认认证；需要 `Full (strict)` 时再配置 TLS Secret。先填写 [deploy/k8s/env/prod-runtime.env.example](k8s/env/prod-runtime.env.example) 的安全副本，然后走受保护的 release/审批流程。

对于 DOKS 生产发布，使用：

~~~bash
DIGITALOCEAN_DEPLOY_ENV=prod \
EXPECTED_CONTEXT=digitalocean-prod \
NAMESPACE=manor-digitalocean-prod \
HOST=app.example.com \
IMAGE_REGISTRY=ghcr.io/manor-os \
IMAGE_TAG=<immutable-tag> \
REGISTRY_USERNAME=replace-me \
REGISTRY_PASSWORD=replace-me \
K8S_APP_ENV_FILE=/secure/manor/prod-app.env \
K8S_DIGITALOCEAN_RUNTIME_SECRET_FILE=/secure/manor/prod-runtime.env \
REQUIRE_BACKUP_SECRET_FILE=false \
SANDBOX_SERVICE_URL=http://10.0.0.10:8000 \
scripts/k8s_digitalocean_apply.sh
~~~

已由发布流水线准备好 production overlay、ExternalSecret 与镜像时，才使用通用生产 apply：

~~~bash
EXPECTED_CONTEXT=<production-context> \
NAMESPACE=manor-prod \
EXPECTED_HOST=app.example.com \
EXPECTED_TLS_SECRET=manor-prod-tls \
scripts/k8s_production_apply.sh
~~~

生产迁移只保证向前执行。不要用旧镜像回滚数据库 schema；采用 expand/contract migration，并在发布前完成备份恢复演练。完整 DOKS 操作见 [docs/DIGITALOCEAN_DOKS_PRODUCTION_DEPLOYMENT_ZH.md](../docs/DIGITALOCEAN_DOKS_PRODUCTION_DEPLOYMENT_ZH.md) 和 [docs/DIGITALOCEAN_DOKS_BACKUP_RESTORE_ZH.md](../docs/DIGITALOCEAN_DOKS_BACKUP_RESTORE_ZH.md)。

## 5. 常用排查

面向同事的 DOKS Test / Prod `kubectl` 配置、只读排障、日志收集、Sandbox runner 和 AI 协助边界见 [DOKS_TROUBLESHOOTING_ZH.md](DOKS_TROUBLESHOOTING_ZH.md)。

~~~bash
# 资源与事件
kubectl --context <context> -n <namespace> get deployments,pods,jobs,pvc
kubectl --context <context> -n <namespace> get events --sort-by=.lastTimestamp

# migration 失败时
kubectl --context <context> -n <namespace> logs job/manor-migration --all-containers=true --tail=200

# 单个服务日志
kubectl --context <context> -n <namespace> logs deployment/manor-api --tail=200
kubectl --context <context> -n <namespace> logs deployment/manor-chat --tail=200
kubectl --context <context> -n <namespace> logs deployment/manor-worker --tail=200
~~~

先检查 migration Job，再检查有状态依赖的 PVC、Secret/ConfigMap 与应用 Pod 日志。不要通过删除 PVC 或 namespace 来处理普通启动失败，这会销毁或重置数据。
