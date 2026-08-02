# 架构详解

## 1. 设计动机

单个 AI 编码 Agent 有固有的盲区：
- **CC (Claude Code)**：擅长理解架构、发现设计缺陷，但实现时可能过度设计
- **OpenCode**：执行快、改动精准，但可能遗漏边界情况
- **Codex**：审核严格，能发现"静默失败"类缺陷（不报错但行为降级）

三体协奏让三者**相互制衡**：CC 审设计防过度设计 → OpenCode 精准实现 → Codex 找隐藏缺陷 → 多轮循环直到通过。

## 2. 编排层（acp_bridge.py）

### 2.1 任务生命周期

```
dispatch → task_id 创建 → 入队（semaphore 限流）→ 执行（subprocess）
  → 流式读取（4KB 块 + 心跳）→ 完成/超时 → 结果存储 → 查询
```

### 2.2 双超时模型

| 超时 | 默认 | 用途 |
|:-----|:-----|:-----|
| `total_timeout` | 900s | 防死锁：任务总时长上限 |
| `idle_timeout` | 120s (CC: 180s) | 判卡死：无输出字节的时长上限 |

两个超时独立判定，任一触发即 kill 进程组（`SIGTERM` 宽限 10s → `SIGKILL`），返回 `reason` 字段（`idle_timeout` / `total_timeout` / `error`）。

### 2.3 流式读取（v2）

- 块读 4KB + 有界队列（64）→ 不会内存爆炸
- `asyncio.wait` 三路汇合：`gather 完成` / `idle watchdog` / `total wait`
- `/tasks/:id` 返回环形缓冲（50×200 字符）心跳 progress

### 2.4 熔断器

```
连续 3 次失败 → degraded → dispatch 返回 503
  → 60s 后半开：放行 1 个探测任务
  → 成功 → 恢复；失败 → 继续 degraded
```

### 2.5 安全

- 请求体 8MB 上限
- prompt 10,000 字符上限（防 DoS）
- 可选 Bearer 认证（`ACP_TOKEN`）
- 输出 5MB 上限（丢最旧保尾部）

## 3. 工具层（mcp_server.py）

标准 MCP (Model Context Protocol) stdio 服务器，提供 4 个工具：

| 工具 | 说明 |
|:-----|:-----|
| `read_file` | 读取文件内容 |
| `search_files` | 正则搜索文件内容 / 按名找文件 |
| `terminal` | 执行终端命令 |
| `get_project_info` | 项目结构总览（根目录 + 文件统计）|

三个 Agent 通过各自的原生 MCP 支持连接（CC: ~/.claude.json、OpenCode: opencode.jsonc、Codex: config.toml）。

## 4. 模型层（opencodex）

[opencodex](https://github.com/bitkyc08/opencodex) 是通用 provider 代理：把 Codex 的 Responses API 请求翻译成任意后端（DeepSeek/Claude/Gemini）。

**为什么需要**：Codex CLI 只用 `wire_api=responses`，与 DeepSeek 的 chat/completions 不兼容。opencodex 作为中间层解决此问题，让三个 Agent 统一走同一模型层。

## 5. 已知限制

- **并行需预拆分**：`opencode run` 是单个子进程，无法中途分裂。大任务需派发前拆 N 组。
- **Codex app-server 模式的 MCP 初始化挂起**（上游 bug，issue #33143）：插件类命令（如 codex-plugin-cc 的 /codex:review）不可用，用 `codex exec` 模式绕过。
- **OpenCode 大任务收尾可能超时**：超时后先查 `git status` 判断文件是否已写入。

## 6. 版本历史

| 版本 | 变更 |
|:-----|:-----|
| v2 (2026-08-02) | 流式读取 + 双超时 + 并行 + 熔断半开 + 内存防护 |
| v1 | 基础派发 + 重试 + 单超时 |
