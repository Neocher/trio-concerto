# 三体协奏 — 经验总结

> 写给在云电脑上部署的另一台 Hermes
> 基于 2026-07-30 整日的迭代调试和评估

---

## 1. 架构全景

```
用户任务
    │
    ▼  [可选] graphify 图谱化 → 注入项目结构上下文
    │
    ▼  [自动] ensure_litellm() → LiteLLM :53684 (CC 依赖)
    │
    ▼  [Stage 1] CC 思考分析 → claude -p "方案设计"
    │
    ▼  [Stage 2] OpenCode 编码 → opencode run --pure -m deepseek-direct/deepseek-v4-flash
    │                     代码落地到 .trio_code_output.py
    ▼  [Stage 3] Codex 审核 → codex exec -s danger-full-access ...
    │
    └── 不通过 → 循环修正（带上轮代码 + 审查反馈，最多 MAX_ITERATIONS 轮）
         └── 通过 → 输出
```

**三个 agent 共享同一个后端模型**（DeepSeek V4 Flash），区别在工具的 prompt 和行为不同。

---

## 2. 关键修复记录

### 2.1 CC 卡死问题

**现象**: `claude -p` 启动后一直 hang，无输出。

**根因**: **LiteLLM 没启动**。CC 的 `.claude/settings.json` 配置了 `ANTHROPIC_BASE_URL=http://127.0.0.1:53684`，但 LiteLLM 未运行。CC 尝试连接 Anthropic 后端（在中国被墙），ConnectionRefused 后超时。

**不是 Ghostty 的问题**：虽然 stderr 打印了 Ghostty shell integration 信息，但那只是父 shell 的 .bashrc 输出，不影响 CC 本身。

**修复**: `trio-concerto.py` 新增 `ensure_litellm()`：
- 启动时检查 `:53684/health`
- 未运行则自动 `litellm --config ~/.hermes/litellm-config.yaml --port 53684`
- 等最多 20s 确认健康

### 2.2 OpenCode 第 1 轮输出为空

**现象**: 第 1 轮 OpenCode 只写了文件系统（`/tmp/opencode/safe_csv.py`），没在回复中吐出代码。导致 `best_code_output` 只有 114 字符。

**修复**: prompt 末尾加强制指令：
```
重要：请直接在回复中输出完整的最终代码（用 markdown 代码块包裹），不要只写文件路径。
```

### 2.3 修正轮次死循环

**现象**: Codex 审查一直"不通过"，永远修正到最大轮数。最终代码被审查意见淹没。

**修复**:
1. 放宽判断逻辑：扫前 500 字符（不是 100），`有条件通过` 算通过
2. 修正轮次中给 OpenCode 附加上一轮的实际代码，不是只给文本反馈
3. 代码落地 `.trio_code_output.py`，供后续 agent 读

### 2.4 MCP SDK v1→v2 迁移

**现象**: `from mcp.server import MCPServer` 报 `ImportError`。

**根因**: MCP SDK v1.x（1.26.0）用的是 `FastMCP`，v2.0.0 才改名 `MCPServer`。

**修复**: `pip install 'mcp>=2.0.0' --break-system-packages`

### 2.5 Codex flag 变更

Codex 0.146.0-alpha.2 → 0.146.0 stable：
```
旧: -c sandbox=danger-full-access -c ask_for_approval=never
新: -s danger-full-access --dangerously-bypass-approvals-and-sandbox
```

---

## 3. 配置清单（一个都不能少）

### 3.1 三路 agent 安装

```bash
# Node >= 22.0.0
nvm install 24 && nvm alias default 24

npm install -g @openai/codex@latest
npm install -g @anthropic-ai/claude-code@latest
npm install -g oh-my-opencode-slim@latest
```

### 3.2 Python 依赖

```bash
pip install 'mcp>=2.0.0' litellm graphifyy --break-system-packages
```

### 3.3 API Key 三处同步

```
~/.bashrc:                     export DEEPSEEK_API_KEY=sk-...
~/.config/responses2chat.env:  DEEPSEEK_API_KEY=sk-...
~/.hermes/.env:                DEEPSEEK_API_KEY=sk-...
```

**坑**: DeepSeek API Key 过期时必须**三处同时更新**（实测 2026-07-28 翻过一次车）。

### 3.4 CC → LiteLLM 配置

`~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:53684",
    "ANTHROPIC_API_KEY": "sk-bc4...e664",
    "ANTHROPIC_MODEL": "claude-sonnet-4-7"
  }
}
```

### 3.5 LiteLLM 配置

`~/.hermes/litellm-config.yaml`:
```yaml
litellm_settings:
  drop_params: true           # 必须加！否则 Anthropic 参数发 DeepSeek 会 400
model_list:
  - model_name: claude-sonnet-4-7
    litellm_params:
      model: openai/deepseek-chat
      api_key: os.environ/DEEPSEEK_API_KEY
      api_base: https://api.deepseek.com/v1
```

### 3.6 Codex → responses2chat 配置

`~/.codex/config.toml`:
```toml
model = "deepseek-v4-flash"
[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:53683/v1"
wire_api = "responses"
```

### 3.7 OpenCode 直连 DeepSeek

`~/.config/opencode/opencode.json`:
```json
"deepseek-direct": {
  "models": { "deepseek-v4-flash": { ... } },
  "name": "DeepSeek Direct",
  "options": {
    "apiKey": "sk-...",
    "baseURL": "https://api.deepseek.com/v1"
  }
}
```

默认模型设为: `"model": "deepseek-direct/deepseek-v4-flash"`

---

## 4. 端口和服务

| 端口 | 服务 | 用途 | 自动管理 |
|:----:|------|------|:-------:|
| 53683 | responses2chat | Codex Responses→Chat | ❌ 需要手动启 |
| 53684 | LiteLLM | CC Anthropic→OpenAI | ✅ trio-concerto 自动启 |

**待改进**: trio-concerto 目前只自动启了 LiteLLM，没自动启 responses2chat。如果 Codex 阶段卡住，先检查 53683 有没有在跑。

---

## 5. 评估结果

### 测试任务: read_csv_safe (简单函数)

| 维度 | 单 Agent (OpenCode) | 三体协奏 |
|------|:------------------:|:--------:|
| 耗时 | 66s | 190s |
| 代码量 | 573 chars | 2,233 chars |
| 修正轮数 | — | 2 轮 |
| FileNotFoundError | ❌ pathlib 预检 | ✅ try/except |
| 编码回退 | ✅ | ✅ |
| 异常类型保留 | ❌ | ✅ raise_on_error |

**结论**:
- 简单任务 → 单 agent 更快（66s vs 190s），质量差距不大
- 严谨场景 → 三体协奏的价值在于 CC 分析方案 + Codex 审查循环
- 瓶颈不在模型（都是 DeepSeek V4 Flash），在流程开销

### 已知问题

1. **responses2chat 不自启** — Codex 阶段依赖它做协议转换
2. **OpenCode 第 1 轮输出不稳** — 偶发性只写文件不吐代码，已加 prompt 加固但未完全杜绝
3. **`review_preview` 可能混入代码预览字段** — `best_code_output` 在修正轮中可能被 OpenCode 输出的审查回复污染
4. **总耗时长** — 3 阶段 × 各 30s~60s + 修正轮次 = 3~5 分钟

---

## 6. 给云电脑部署的建议

### 6.1 目录结构

```
trio-concerto/
├── trio-concerto.py        # 主编排脚本（核心文件）
├── mcp-bridge-v5.py       # MCP 三向桥接
├── litellm-config.yaml    # LiteLLM 配置模板（需填 API key）
├── responses2chat.py      # Responses→Chat 转换代理
├── setup.sh               # 一键部署脚本
├── requirements.txt       # Python 依赖
└── HERMES_NOTES.md        # 本文件
```

### 6.2 setup.sh 需要做的事

```bash
1. 检测 Node 版本（要求 >= 22）
2. npm install -g 三个 agent
3. pip install Python 依赖
4. 写入 ~/.claude/settings.json
5. 写入 ~/.codex/config.toml
6. 写入 /etc/privoxy/config（如果翻墙需要）
7. 注册 trio-concerto 到 Hermes MCP
8. 提示用户填写 DEEPSEEK_API_KEY
```

### 6.3 推荐的启动顺序

```bash
# 1. 先确保翻墙代理正常工作
# 2. 启动 responses2chat（Codex 依赖）
python3 ~/trio-concerto/responses2chat.py --port 53683 &

# 3. 注册 MCP
hermes mcp add trio-concerto --command mcp --args run ~/trio-concerto/trio-concerto.py

# 4. 直接调用
trio_concerto(task="...", workdir="...")
# LiteLLM 会在 trio-concerto 内部自动启动
```

### 6.4 调试指南

| 症状 | 排查 |
|------|------|
| CC 卡住 | 检查 `:53684/health`，重启 LiteLLM |
| Codex 卡住 | 检查 `:53683` responses2chat 是否在跑 |
| OpenCode 报 401 | DeepSeek API key 是否过期？三处全更新 |
| OpenCode 报 agent-plan 过期 | 确保 `-m deepseek-direct/deepseek-v4-flash` |
| OpenCode 首次超慢 | 首次加载模型可能需要 10s+ 预热 |
| 修正死循环 | 检查 Codex 审查输出是否带 `## 通过状态: 有条件通过` |
