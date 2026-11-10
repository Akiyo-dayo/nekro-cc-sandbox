# nekro-cc-sandbox

`nekro-cc-sandbox` 是一个基于 **Claude Code** 的持久化工作区沙盒服务，用于为上层 Agent 系统提供可隔离、可持续、可流式返回的任务执行环境。

## 本 fork 相对上游改了什么

这是 [NekroAI/nekro-cc-sandbox](https://github.com/NekroAI/nekro-cc-sandbox) 的 fork，起点是上游提交 `0508095`（0.1.14），许可证仍是 AGPL-3.0。改动只在镜像构建上，`src/` 下的 Python 代码与上游相同：

- **Claude Code 钉版本**：Dockerfile 里的 `ARG CLAUDE_CODE_VERSION`，当前是 2.1.311（npm 的 stable 通道）。构建时核对 `claude --version`，对不上就失败；镜像里设了 `DISABLE_AUTOUPDATER=1`，CLI 不会在运行中自己升级。
- **Node.js 20 → 22**：Claude Code 从 2.1.198 起要求 Node ≥ 22，Node 20 也已在 2026-04-30 停止维护。前端构建阶段同样换成 `node:22-alpine`。
- **其余组件也钉版本**：`agent-browser` 0.23.4、`playwright` 1.58.2（决定 Chromium 版本）、`pnpm` 10.33.0（只用来构建前端）。取的是上游 0.1.14 镜像构建时（2026-03-31）的版本，行为与上游镜像一致。
- **Debian 包钉版本**（ak2 起）：基础镜像固定在 `python:3.13-slim-trixie`；`openssh-client`、`rsync`、`git`（ak3 起还有 `tini`）钉到具体版本，见 Dockerfile 里的 `OPENSSH_CLIENT_VERSION` / `RSYNC_VERSION` / `GIT_VERSION` / `TINI_VERSION`。`rsync` 是新增的。Debian 每个源里只保留最新版，所以出了安全更新以后，旧版本会从源里消失，构建会失败。这是故意的：查到新版本号，改掉 ARG，再发新标签。
- **SSH 客户端配置挂点**（ak2 起）：`/etc/ssh/ssh_config.d/50-nekro.conf` 只有一行 `Include /home/appuser/.claude/nk-ssh/config`。上层应用（例如 Nekro Agent）可以往每个工作区的 `~/.claude/nk-ssh/config` 写 SSH 客户端配置；文件不存在时 ssh 照常工作。注意 OpenSSH 会检查被 Include 文件的属主和权限：属主必须是 root 或运行 ssh 的用户（`appuser`），而且不能对组或其他人可写，否则 ssh 直接报 `Bad owner or permissions`。
- **tini 当 1 号进程**（ak3 起）：`ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/usr/local/bin/docker-entrypoint.sh"]`，`tini` 钉在 `0.19.0-3+b8`。上游的入口脚本最后一步是 `runuser`，它只等自己的子进程。强制取消任务时，整个进程组被 SIGKILL，`script` 先死，`claude` 就成了挂在 1 号进程名下的孤儿，死后没人回收，变成 `<defunct>` 僵尸，而且越积越多。`-s` 让 tini 同时注册成 subreaper，即使运行时另外加了 init 也照样能回收。
- **版本号** `0.1.14+ak3`：`+akN` 是本 fork 的修订号。ak1 是第一版，钉住 Claude Code、Node 和 npm 组件的版本；ak2 加了 Debian 包版本钉和 SSH 配置挂点；ak3 加了 tini。
- **测试工作流**（`.github/workflows/test.yml`）：e2e 任务的 Node 换成 22，pnpm 钉在 10.33.0。原来用的 `latest`（11 以上）装这份锁文件会报 `ERR_PNPM_IGNORED_BUILDS`。
- **发布工作流**（`.github/workflows/publish.yml`）：只构建 linux/amd64，推到 `ghcr.io/akiyo-dayo/nekro-cc-sandbox`。标签形如 `0.1.14-ak3-cc2.1.311`，发布后不覆盖，也不发 `latest`。推送前先启动容器做冒烟检查，检查各组件版本、SSH 配置挂点，以及孤儿进程退出后有没有留下僵尸；只用内置的 `GITHUB_TOKEN`。

升级 Claude Code：改 Dockerfile 里的 `ARG CLAUDE_CODE_VERSION`（其他内容也变了的话，把 `+akN` 加一），提交后手动运行 “Release Docker Image” 工作流，或推送 `v<镜像标签>` 形式的 git 标签。

它适合被用作：

- Claude Code 的 API 化执行后端
- 持久化 workspace / sandbox runtime
- 上层 Agent 编排系统的执行层
- 带会话、工具探测、任务排队能力的工作区服务

如果你在搜索这些关键词：

- `Claude Code sandbox`
- `Claude Code workspace backend`
- `persistent workspace agent`
- `agent sandbox runtime`
- `nekro cc sandbox`

这个项目就是对应实现。

与 **Nekro Agent** 生态的关系：

- `nekro-cc-sandbox` 可以独立理解和开发
- 它同时也是 **[KroMiose/nekro-agent](https://github.com/KroMiose/nekro-agent)** 工作区执行链路的重要基础组件
- 如果你想看完整的多平台 Agent 编排系统，可以继续访问主项目：<https://github.com/KroMiose/nekro-agent>

## 这个项目解决什么问题

Claude Code 很强，但在真实系统里直接把 CLI 裸接到业务层通常会遇到这些问题：

- 缺少稳定的 API 封装
- 会话上下文不易持久化
- 多任务并发时容易互相干扰
- 缺少工作区隔离与状态管理
- 不方便接入上层 WebUI、SSE 和调度系统

`nekro-cc-sandbox` 的作用，就是把 Claude Code 包装成一个更适合系统集成的工作区执行服务：

```text
Client / Agent Orchestrator
  -> nekro-cc-sandbox API
  -> Workspace Manager
  -> Claude Runtime
  -> Claude Code CLI
  -> JSON / SSE / Status / Shell
```

## 核心能力

- **持久化工作区**：每个 workspace 有独立目录、独立会话和独立运行上下文
- **Claude Code Runtime**：通过 `claude -p` 驱动 Claude Code CLI 完成任务执行
- **流式消息接口**：支持 SSE 持续输出文本片段、工具调用和工具结果
- **任务排队机制**：同一工作区内自动串行化任务，避免上下文污染
- **工具能力探测**：独立查询和刷新 Claude Code 当前可用工具列表
- **会话重置**：支持按 workspace 清空当前 Claude 会话
- **运行策略控制**：支持 `agent`、`relaxed`、`strict` 三种 runtime policy
- **交互式 Shell**：用于调试沙盒内真实环境
- **Web 调试界面**：便于本地验证状态、消息和工作区行为
- **待投递结果暂存**：为上层系统断连场景提供容灾缓冲

## 典型场景

- 给上层 AI Agent 系统提供执行层
- 为 Claude Code 构建 HTTP / SSE 服务化封装
- 做持久化 workspace agent 研究
- 做自动化代码任务、文件任务、工具调用任务编排
- 做独立的 Agent sandbox / runtime backend

## 项目结构

```text
nekro_cc_sandbox/
├── src/nekro_cc_sandbox/
│   ├── api/            # FastAPI 路由：消息、状态、SSE、shell
│   ├── claude/         # Claude Runtime 与运行策略
│   ├── workspace/      # 工作区管理
│   ├── shell/          # 交互式 shell
│   ├── store/          # 暂存与状态存储
│   └── main.py         # 应用入口
├── frontend/           # 本地调试前端
├── tests/              # 单元、集成、E2E 测试
├── Dockerfile
└── pyproject.toml
```

## API 概览

常用接口：

- `POST /api/v1/message`
- `POST /api/v1/message/stream`
- `GET /api/v1/capabilities/tools`
- `POST /api/v1/capabilities/tools/refresh`
- `POST /api/v1/workspaces/{workspace_id}/session/reset`
- `GET /api/v1/status`
- `GET /health`

接口职责：

- `/api/v1/message`：非流式调用 Claude Code
- `/api/v1/message/stream`：流式执行任务并返回 SSE 事件
- `/api/v1/capabilities/tools`：读取工具能力缓存
- `/api/v1/capabilities/tools/refresh`：主动刷新工具能力
- `/api/v1/workspaces/{workspace_id}/session/reset`：重置工作区会话

## 本地开发

环境要求：

- Python `>= 3.13`
- `uv`
- Node.js 与 `pnpm`（如需运行前端）
- 可用的 `claude` CLI

安装依赖：

```bash
uv sync --all-extras
```

启动开发服务：

```bash
poe dev
```

运行检查：

```bash
poe lint
poe typecheck
poe test
poe check
```

前端调试：

```bash
poe frontend-install
poe frontend-dev
```

## Docker

构建镜像：

```bash
poe docker-build
```

本地运行：

```bash
poe docker-run
```

如果你是从 **[KroMiose/nekro-agent](https://github.com/KroMiose/nekro-agent)** 生态了解这个项目，通常它会作为工作区沙盒容器由主系统自动管理，而不是手工长期单独运行。

## 环境变量

常用配置项：

- `WORKSPACE_ROOT`：工作区根目录
- `SETTINGS_PATH`：设置文件路径
- `RUNTIME_POLICY`：运行策略，支持 `agent` / `relaxed` / `strict`
- `SKIP_PERMISSIONS`：是否跳过权限确认
- `HOST`：监听地址
- `PORT`：监听端口

默认推荐使用 `agent` 模式，它更适合被上层系统编排调用，而不是给人类直接做交互式终端使用。

## 为什么这个项目适合做执行后端

相比直接调用 Claude Code CLI，`nekro-cc-sandbox` 多提供了几层系统化能力：

- 对外稳定的 HTTP API
- 工作区隔离与目录管理
- 会话生命周期管理
- 流式事件协议
- 任务并发控制
- 状态查询与调试能力

这使它更适合接入 Agent 平台、工作流系统或自定义编排层。

## 生态链接

- 主项目 `KroMiose/nekro-agent`: <https://github.com/KroMiose/nekro-agent>
- 文档站: <https://doc.nekro.ai>
- NekroAI 云社区: <https://cloud.nekro.ai>

## License

请以当前仓库中的 License 文件和相关说明为准。
