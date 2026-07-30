<div align="center">
<h1>三体协奏 — Trio Concerto</h1>
<p><b>三 agent 流水线：CC 思考分析 → OpenCode 编码实现 → Codex 审核验证</b></p>
<p>Hermes 调度，三体协同，循环修正</p>
</div>

---

## 架构

```
用户任务
    │
    ▼  [可选] graphify 图谱化
    │
    ▼  [自动] LiteLLM (:53684) — CC 协议转换代理
    │
    ▼  [Stage 1] CC 思考分析       → 架构方案
    │
    ▼  [Stage 2] OpenCode 编码实现  → 代码（落地 .trio_code_output.py）
    │
    ▼  [Stage 3] Codex 审核验证     → 审查意见
    │
    └── 不通过 → 循环修正（带上轮代码，最多 3 轮）
         └── 通过 → 最终输出
```

三个 agent 共享同一个后端模型（DeepSeek V4 Flash），通过不同的 prompt 和工具行为实现分工协作。

## 快速开始

### 前提条件

| 依赖 | 版本要求 | 安装方式 |
|------|---------|---------|
| Node.js | >= 22 | `nvm install 24` |
| Python | >= 3.10 | 系统自带 |
| Hermes Agent | — | [Hermes](https://hermes-agent.nousresearch.com) |
| DeepSeek API Key | — | `export DEEPSEEK_API_KEY="sk-..."` |

### 一键部署

```bash
git clone https://github.com/Neocher/三体协奏.git
cd 三体协奏
bash setup.sh
```

### 手动安装

```bash
# 1. 三路 agent
npm install -g @openai/codex @anthropic-ai/claude-code oh-my-opencode-slim

# 2. Python 依赖
pip install 'mcp>=2.0.0' litellm graphifyy

# 3. 复制 litellm 配置
mkdir -p ~/.hermes
cp litellm-config.yaml ~/.hermes/

# 4. 注册 Hermes MCP
hermes mcp add trio-concerto --command mcp --args run $(pwd)/trio-concerto.py
```

### 配置

**Claude Code → LiteLLM** (`~/.claude/settings.json`):
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:53684",
    "ANTHROPIC_API_KEY": "sk-...",
    "ANTHROPIC_MODEL": "claude-sonnet-4-7"
  }
}
```

**Codex → responses2chat** (`~/.codex/config.toml`):
```toml
model = "deepseek-v4-flash"
[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:53683/v1"
wire_api = "responses"
```

### 启动

```bash
# 1. 启动 responses2chat 协议转换代理（Codex 依赖）
python3 responses2chat.py --port 53683 &

# 2. 直接调用三体协奏
trio_concerto(task="实现一个函数", workdir="/path/to/project")

# 或带 graphify 知识图谱
trio_concerto(task="重构用户模块", workdir="/path", graphify=True)
```

LiteLLM 会在 trio-concerto 内部自动启动。

## API

### `trio_concerto(task, workdir, graphify)`

| 参数 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `task` | str | ✅ | 任务描述 |
| `workdir` | str | ❌ | 工作目录 |
| `graphify` | bool | ❌ | 是否先构建知识图谱 |

### `trio_status()`

返回当前三路 agent 的状态和流水线信息。

## 评估结果

简单函数（read_csv_safe）实测：

| 维度 | 单 Agent | 三体协奏 |
|------|:--------:|:--------:|
| 耗时 | 66s | 190s（含 2 轮修正） |
| FileNotFoundError | ❌ | ✅ |
| 编码回退 | ✅ | ✅ |
| 异常类型保留 | ❌ | ✅ |

三体协奏的价值在于 CC 的架构分析 + Codex 的审查循环，适合代码库级重构和严谨场景。简单任务建议直接用单 agent。

## 已知问题

1. **responses2chat 不自启** — Codex 阶段依赖 :53683，需手动启动
2. **总耗时较长** — 3 阶段 + 修正轮次 = 3~5 分钟
3. **依赖 DeepSeek** — 若需更换模型需改多处配置

## 许可

MIT
