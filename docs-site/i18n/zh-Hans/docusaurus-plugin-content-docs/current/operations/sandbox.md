---
title: 沙箱
---

# 沙箱

公开 native browser 和 Code Projects 执行路径使用自托管 sandbox；不需要也不包含
私有 Chrome 扩展/CLI runtime。缺少 sandbox 配置应只处理依赖它的工具，不应禁用
无关工作区能力。商业额度放行不绕过 sandbox 资源限制、审批或工具范围。

沙箱服务将代码执行与 API 和 worker 容器隔离。当 Agent 运行技能脚本或
shell 命令时，沙箱服务会为本次运行启动一个专用的子 Docker 容器，施加
资源限制并返回输出——因此不受信任的代码永远不会在应用进程内部执行。

## 职责 {#responsibilities}

- 在按运行创建的子容器中执行技能脚本和 shell 工作负载。
- 对每次运行强制执行内存、CPU、进程数和超时限制。
- 提供受限的工作目录，以及（默认情况下）只读的根文件系统。
- 将运行时依赖（解释器、包安装）与 API 镜像隔离开。

## 服务 {#services}

| 服务 | 角色 |
| --- | --- |
| `sandbox` | 管理隔离执行容器的 HTTP 服务。 |
| `sandbox-skill-image` | 构建 `sandbox-skill:latest`，即子容器运行的基础镜像。在 Compose 内部构建它意味着 `docker compose up --build -d` 不需要单独的预构建步骤。 |

## 配置 {#configuration}

API 和 worker 通过以下变量访问沙箱：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SANDBOX_SERVICE_URL` | `http://sandbox:8000` | 沙箱调用的端点。 |
| `SHELL_SANDBOX_ENABLED` | `true` | 允许基于 shell 的 Agent 工具。设为 `false` 可完全禁用 shell 执行。 |
| `SANDBOX_EXEC_QUEUE_WAIT_SECONDS` | `600` | 命令等待用户沙箱空闲槽位的最长时间，超时则答复“未启动”。期间持久化的对话运行会挂起（`waiting_resource` / `sandbox_exec_slot`），命令开始后恢复。 |

沙箱服务本身通过 `sandbox` 容器上的 `SANDBOX_*` 变量进行调优：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SANDBOX_IMAGE` | `sandbox-skill:latest` | 子容器使用的镜像。 |
| `SANDBOX_NETWORK` | `bridge` | 子容器加入的 Docker 网络。 |
| `SANDBOX_DNS_SERVERS` | 未设置 | 子容器的可选 DNS 覆盖。 |
| `SANDBOX_MEMORY` | `1g` | 每个沙箱的内存限制（每个用户一个通用沙箱，由该用户的所有工作共享）。 |
| `SANDBOX_CPUS` | `1.0` | 每次运行的 CPU 限制。 |
| `SANDBOX_PIDS_LIMIT` | `512` | 每个沙箱的最大进程（任务）数。 |
| `SANDBOX_MAX_SANDBOXES` | `5` | 同时存活的子容器数量上限。 |
| `SANDBOX_MEMORY_OVERCOMMIT` | `2.0` | 按内存准入：只有当所有子容器内存上限之和不超过「宿主机内存 × 该系数 − `SANDBOX_MEMORY_RESERVE`」时才创建新容器。 |
| `SANDBOX_MEMORY_RESERVE` | `256m` | 为 Docker、沙箱服务和操作系统预留的内存。 |
| `SANDBOX_MAX_CONTAINER_MEMORY` | 宿主机内存 − 预留 | 单个容器的内存上限；更大的请求会被压到该值（创建响应中带有警告）。 |
| `SANDBOX_OVERRIDE_MAX_MEMORY` / `_CPUS` / `_PIDS` / `_WORKDIR_TMPFS` | `8g` / `8` / `1024` / `4g` | 调用方可请求的上限。只有内存、CPU、进程数、工作目录大小、网络（限于 `SANDBOX_ALLOWED_NETWORKS`）和超时可以覆盖；镜像、挂载和安全设置不可覆盖。 |
| `SANDBOX_OVERRIDE_MAX_EXEC_TIMEOUT` / `_INSTALL_TIMEOUT` | `1800` / `900` | 调用方可请求的命令与安装超时上限。External agent 池 runner 把命令上限设为 `3600`。 |
| `SANDBOX_MAX_CONCURRENT_EXECUTIONS` | `4` | 一个沙箱同时运行的命令数；另受 内存/256 MiB 与 pids/32 约束。 |
| `SANDBOX_MAX_QUEUED_EXECUTIONS` | `64` | 一个沙箱中可排队等待空闲槽位的命令数（跨会话与任务公平的先进先出）。 |
| `SANDBOX_MAX_LIFETIME_SECONDS` | `86400` | 沙箱超过该时长后，在没有命令运行或等待时回收（`0` 表示从不）。其中文件是临时的。 |
| `SANDBOX_DISK_WATCHDOG_INTERVAL_SECONDS` | `3` | 运行器按工作区域测量有任务的沙箱磁盘的间隔（`0` 关闭磁盘看守）。 |
| `SANDBOX_WORKDIR_TMPFS_SIZE` / `SANDBOX_WORKSPACE_TMPFS_SIZE` | `256m` / `256m` | `/skills`（并存加载的技能）与 `/workspace`（每个会话或任务一个区域）的大小。 |
| `SANDBOX_TMP_TMPFS_SIZE` / `SANDBOX_HOME_TMPFS_SIZE` | `64m` / `64m` | `/tmp`、`/var/tmp` 与沙箱用户主目录的大小。所有 tmpfs 都计入 `SANDBOX_MEMORY`。 |
| `SANDBOX_READ_ONLY_ROOT` | `true` | 以只读方式挂载子容器的根文件系统。 |
| `SANDBOX_WORKDIR` | `/skill` | 子容器内可写的工作目录。 |
| `SANDBOX_INSTALL_TIMEOUT` | `300` | npm/pip 安装允许的秒数。 |


## 运维指引 {#operational-guidance}

- 只在 Docker 网络内部暴露沙箱端点；栈外的任何东西都不应能访问它。
- 将 shell 执行视为敏感能力：将其与 Agent 工具作用域和
  [人工审批（HITL）治理](../concepts/hitl-governance.md)配合使用，让使用
  shell 的 Agent 在审批策略下运行。
- 子容器以 `skill-sbx-` 前缀命名；如果某次运行被非正常终止，可以用
  `docker ps -a --filter name=skill-sbx-` 列出并清理残留容器。
- 如果合法技能触碰到限制，可调高 `SANDBOX_MEMORY` / `SANDBOX_CPUS`；
  默认值优先保护宿主机。
- 单个命令无法拖垮共享沙箱：超出内存限制的命令会被单独终止并报告为内存不足；
  沙箱接近进程数上限时，失控的进程树会被整体停止；`/workspace` 接近写满（92%）时，
  占用最多的工作区域中的命令会被停止（`disk_killed`），该区域的新命令在释放空间前
  会被拒绝且不会启动（`rm` 等清理命令仍可运行）；其他命令照常运行，磁盘写满也不会
  回收共享沙箱。超出槽位上限的命令会排队等待，而不是直接失败。
