# Trio Concerto — 三体协奏：多 Agent 协同编码编排框架

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

**三体协奏（Trio Concerto）** 是一个多 Agent 协同编码编排框架——用 **CC (Claude Code) → OpenCode → Codex** 三个 AI 编码 Agent 组成"设计 → 编码 → 审核"的完整研发流水线，通过一个轻量 HTTP 编排层（ACP Bridge）统一调度。

> 名字来源：三个 Agent 像三体运动一样相互制衡、缺一不可——CC 审设计、OpenCode 写代码、Codex 查问题，形成闭环。

---

## 🏗️ 架构

```
┌─────────────────────────────────────────────────────────┐
│  调用方（Hermes / 任意 HTTP 客户端）                     │
│    │ POST /dispatch → ACP Bridge (:8770)                │
│    │ GET /tasks/:id（实时心跳 progress）                 │
├─────────────────────────────────────────────────────────┤
│  工具层: MCP shm-tools (mcp_server.py)                  │
│    read_file / search_files / terminal / get_project_info│
├─────────────────────────────────────────────────────────┤
│  模型层: opencodex (:10100) → DeepSeek v4-flash          │
│    （统一 Responses API 代理，三 Agent 共享）            │
├─────────────┬───────────────┬───────────────────────────┤
│  CC          │  OpenCode      │  Codex                    │
│  claude -p   │  opencode run  │  codex exec               │
│  idle 180s   │  idle 120s     │  idle 120s                │
│  并发 2      │  并发 3        │  并发 3 (与OC共享sem)     │
└─────────────┴───────────────┴───────────────────────────┘
```

### 三层协议分工

| 层 | 组件 | 协议 | 职责 |
|:---|:-----|:-----|:-----|
| **编排层** | acp_bridge.py (:8770) | HTTP + subprocess | 任务派发、双超时、熔断、心跳、结果收集 |
| **工具层** | mcp_server.py | MCP (stdio) | 文件读取、搜索、终端执行，三 Agent 共享 |
| **模型层** | opencodex (:10100) | Responses API 代理 | 统一 DeepSeek/Claude/Gemini 等后端 |

## 🔄 标准工作流

```
1. 设计（Phase 1）:  CC 审查设计 → 发现设计问题先修
2. 编码（Phase 2）:  OpenCode 实现（基于 CC 评审结果手术式修改）
3. 审核（Phase 3）:  Codex 验证（可多轮，直到输出"最终通过"）
4. 本机验证:         pytest 全量 + 语法检查
5. 提交:             git commit（三段式：根因→修复→验证）
```

## 🚀 快速开始

### 依赖

- Python 3.10+
- [opencodex](https://github.com/bitkyc08/opencodex)（模型层代理，可选——也可直接配 DeepSeek/其他）
- Claude Code CLI、OpenCode CLI、Codex CLI（三个 Agent）

### 安装

```bash
git clone https://github.com/<you>/trio-concerto.git
cd trio-concerto
pip install -r requirements.txt
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|:-----|:-------|:-----|
| `SHM_WORKDIR` | 当前文件目录 | Agent 工作目录（放你的项目）|
| `OPENCODE_BIN` | `~/.hermes/node/bin/opencode` | OpenCode 二进制路径 |
| `ACP_TOKEN` | 空（不认证）| Bearer 认证 token |
| `SHM_AGENT_TOTAL_TIMEOUT` | 900 | 总超时（秒）|
| `SHM_AGENT_IDLE_TIMEOUT` | 120 | 空闲超时（秒），CC 建议 180 |
| `SHM_AGENT_CC_CONCURRENCY` | 2 | CC 并发 |
| `SHM_AGENT_OC_CONCURRENCY` | 3 | OpenCode 并发 |
| `SHM_AGENT_CODEX_CONCURRENCY` | 3 | Codex 并发 |

### 启动

```bash
# 模型层（如用 opencodex）
ocx config set defaultProvider deepseek
ocx start --port 10100

# 编排层
python3 acp_bridge.py
```

### 使用

```bash
# 派发任务
curl -X POST http://127.0.0.1:8770/dispatch \
  -H "Content-Type: application/json" \
  -d '{"target_agent":"opencode","prompt":"重构 xxx 模块"}'

# 查询任务（含心跳 progress）
curl http://127.0.0.1:8770/tasks/<task_id>
```

## 📡 API

| 方法 | 端点 | 说明 |
|:-----|:-----|:-----|
| GET | `/health` | 摘要 + 熔断状态 |
| GET | `/agents` | 各 Agent 健康详表 |
| POST | `/dispatch` | 派发任务 `{target_agent, prompt}` |
| GET | `/tasks/:id` | 任务状态 + 心跳 progress |
| POST | `/reset/:agent` | 手工恢复熔断 |

## 🛡️ 韧性特性

| 能力 | 说明 |
|:-----|:-----|
| 流式读取 | 4KB 块读 + 有界队列(64) + 三路汇合，实时心跳 |
| 双超时 | 总超时 900s（防死锁）+ 空闲超时 120/180s（判卡死）|
| 进程组击杀 | 超时 kill 整个进程树，不留僵尸 |
| 熔断 + 半开 | 连续 3 次失败 → degraded → 60s 后半开探测 |
| 内存防护 | 输出 5MB 上限 + 截断 100KB + in-flight ≤ 20 |
| 认证 | 可选 Bearer token（`ACP_TOKEN`）|
| 任务清理 | 每 5 分钟清理 > 30 分钟任务 |

## 📂 目录

```
trio-concerto/
├── acp_bridge.py        # 编排层（核心）
├── mcp_server.py        # 工具层（MCP 服务器）
├── mcp_orchestrator.py  # MCP 编排适配层（Hermes 入口：三阶段流水线 → HTTP 编排）
├── requirements.txt
├── docs/
│   └── architecture.md   # 架构详解
├── scripts/
│   └── dispatch.sh       # 派发示例脚本
└── README.md
```

## 🤝 MCP 编排入口（mcp_orchestrator.py）

Hermes（或任意 MCP 客户端）通过 `mcp_orchestrator.py` 调用**完整三阶段流水线**，
内部经 acp_bridge HTTP 编排：CC 思考分析 → OpenCode 编码实现 → Codex 审核验证
（不合格循环修正，最多 3 轮；CC 失败自动 OpenCode 兜底）。

```bash
hermes mcp add trio-concerto --command mcp --args run /home/user/trio-concerto/mcp_orchestrator.py
```

工具：`trio_concerto(task, workdir, graphify)` 提交（立即返回 task_id）→
`trio_result(task_id)` 轮询进度/结果 → `trio_list()` 全部任务 → `trio_status()`
各 agent 健康。依赖 `acp_bridge` 运行于 :8770（可用环境变量 `TRIO_BRIDGE` 覆盖）。

## 📜 License

Apache-2.0 © 2026
