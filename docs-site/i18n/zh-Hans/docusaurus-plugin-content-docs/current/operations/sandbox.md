---
title: 沙箱
---

# 沙箱

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

沙箱服务本身通过 `sandbox` 容器上的 `SANDBOX_*` 变量进行调优：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SANDBOX_IMAGE` | `sandbox-skill:latest` | 子容器使用的镜像。 |
| `SANDBOX_NETWORK` | `bridge` | 子容器加入的 Docker 网络。 |
| `SANDBOX_DNS_SERVERS` | 未设置 | 子容器的可选 DNS 覆盖。 |
| `SANDBOX_MEMORY` | `512m` | 每次运行的内存限制。 |
| `SANDBOX_CPUS` | `1.0` | 每次运行的 CPU 限制。 |
| `SANDBOX_PIDS_LIMIT` | `256` | 每次运行的最大进程数。 |
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
