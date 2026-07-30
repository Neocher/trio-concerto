#!/usr/bin/env bash
set -euo pipefail

# ─── 三体协奏 — 一键部署 ───────────────────────────────────────
# 用法: bash setup.sh
# 依赖: Node >= 22.0.0, Python 3.10+, 已安装 Hermes Agent

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }

echo "========================================"
echo "  三体协奏 — 一键部署"
echo "========================================"

# ─── 1. 检查 Node 版本 ─────────────────────────────────────────
NODE_VER=$(node --version 2>/dev/null || echo "none")
if [ "$NODE_VER" = "none" ]; then
    err "Node.js 未安装，请先安装 Node >= 22"
    exit 1
fi
NODE_MAJOR=$(echo "$NODE_VER" | sed 's/v//' | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 22 ]; then
    warn "Node $NODE_VER 版本过低，建议升级到 22+"
fi
info "Node $NODE_VER"

# ─── 2. 安装 / 检查三个 agent ──────────────────────────────────
PACKAGES=""
command -v codex    >/dev/null 2>&1 || PACKAGES="$PACKAGES @openai/codex"
command -v claude   >/dev/null 2>&1 || PACKAGES="$PACKAGES @anthropic-ai/claude-code"
command -v opencode >/dev/null 2>&1 || PACKAGES="$PACKAGES oh-my-opencode-slim"

if [ -n "$PACKAGES" ]; then
    info "安装:$PACKAGES"
    npm install -g $PACKAGES
else
    info "三个 agent 均已安装"
fi

# ─── 3. 安装 Python 依赖 ────────────────────────────────────────
info "安装 Python 依赖..."
pip install -r "$REPO_DIR/requirements.txt" --break-system-packages 2>/dev/null || \
pip install -r "$REPO_DIR/requirements.txt"

# ─── 4. 检查 DeepSeek API Key ────────────────────────────────────
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    warn "DEEPSEEK_API_KEY 未设置"
    warn "请在 ~/.bashrc 中添加: export DEEPSEEK_API_KEY=\"sk-...\""
    warn "或创建 ~/.config/responses2chat.env: DEEPSEEK_API_KEY=sk-..."
else
    info "DeepSeek API key 已设置"
fi

# ─── 5. 注册到 Hermes MCP ──────────────────────────────────────
if command -v hermes >/dev/null 2>&1; then
    if ! hermes mcp list 2>/dev/null | grep -q trio-concerto; then
        info "注册 trio-concerto 到 Hermes MCP..."
        hermes mcp add trio-concerto \
            --command mcp \
            --args run "$REPO_DIR/trio-concerto.py"
    else
        info "trio-concerto 已注册到 Hermes MCP"
    fi
else
    warn "Hermes 未安装，跳过 MCP 注册"
fi

# ─── 6. 配置文件模板提示 ────────────────────────────────────────
echo ""
echo "========================================"
echo "  安装完成！"
echo "========================================"
echo ""
echo "下一步需要配置:"
echo ""
echo "1. Claude Code → LiteLLM:"
echo "   创建 ~/.claude/settings.json:"
echo '   { "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:53684",'
echo '            "ANTHROPIC_MODEL": "claude-sonnet-4-7" } }'
echo ""
echo "2. Codex → responses2chat:"
echo "   创建 ~/.codex/config.toml:"
echo '   model = "deepseek-v4-flash"'
echo '   [model_providers.deepseek]'
echo '   name = "DeepSeek"'
echo '   base_url = "http://127.0.0.1:53683/v1"'
echo '   wire_api = "responses"'
echo ""
echo "3. 启动 responses2chat 代理:"
echo "   python3 $REPO_DIR/responses2chat.py --port 53683 &"
echo ""
echo "4. 查看完整文档:"
echo "   cat $REPO_DIR/HERMES_NOTES.md"
echo ""
