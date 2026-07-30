#!/usr/bin/env python3
"""
MCP Agent Bridge v6 — 基于 FastMCP
CC ↔ Codex ↔ OpenCode 三向 MCP 桥接

用法:
  MCP_BRIDGE_TARGET=codex    mcp run /home/user/mcp-bridge-v5.py
  MCP_BRIDGE_TARGET=claude   mcp run /home/user/mcp-bridge-v5.py
  MCP_BRIDGE_TARGET=opencode mcp run /home/user/mcp-bridge-v5.py
"""

import asyncio
import json
import logging
import os
import sys
import urllib.request

from mcp.server import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] mcp-bridge: %(message)s",
)
logger = logging.getLogger("mcp-bridge")

MAX_PROMPT_LEN = 10000
AGENT_TIMEOUT = 180

AGENT_CONFIGS = {
    "claude": {
        "name": "Claude Code",
        "exec_cmd": ["claude", "-p"],
        "description": "代码审查、架构设计",
        "prompt_style": "stdin",
        "needs_litellm": True,
    },
    "codex": {
        "name": "Codex",
        "exec_cmd": [
            "codex", "exec",
            "--model", "deepseek-v4-flash",
            "-c", "sandbox=danger-full-access",
            "-c", "ask_for_approval=never",
            "--skip-git-repo-check",
        ],
        "description": "代码生成、重构优化",
        "prompt_style": "stdin",
    },
    "opencode": {
        "name": "OpenCode",
        "exec_cmd": ["opencode", "run", "--pure", "-m", "deepseek-direct/deepseek-v4-flash"],
        "description": "多文件编辑、项目级变更",
        "prompt_style": "arg",
    },
}


async def ensure_litellm() -> bool:
    """Start LiteLLM if needed (CC depends on it)."""
    LITELLM_PORT = 53684
    LITELLM_CONFIG = os.path.expanduser("~/.hermes/litellm-config.yaml")
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{LITELLM_PORT}/health")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        pass
    if not os.path.exists(LITELLM_CONFIG):
        logger.warning("LiteLLM config not found: %s", LITELLM_CONFIG)
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "litellm", "--config", LITELLM_CONFIG, "--port", str(LITELLM_PORT),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        for i in range(10):
            await asyncio.sleep(2)
            try:
                urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{LITELLM_PORT}/health"),
                    timeout=2
                )
                return True
            except Exception:
                continue
        proc.kill()
        return False
    except Exception as e:
        logger.error("LiteLLM start failed: %s", e)
        return False


async def run_agent(agent_id: str, prompt: str, workdir: str | None = None) -> dict:
    """在目标 agent 上执行任务（prompt 通过 stdin 传递）"""
    config = AGENT_CONFIGS.get(agent_id)
    if not config:
        return {"error": f"未知 agent: {agent_id}"}
    if len(prompt) > MAX_PROMPT_LEN:
        return {"error": f"prompt 过长: {len(prompt)} > {MAX_PROMPT_LEN}"}

    # 自动启动依赖服务
    if config.get("needs_litellm"):
        if not await ensure_litellm():
            return {"error": "LiteLLM 启动失败，CC 无法运行"}

    cmd = config["exec_cmd"]
    if config.get("prompt_style") == "arg":
        cmd = list(cmd) + [prompt]
    cwd = workdir or os.getcwd()
    if workdir and not os.path.isdir(workdir):
        return {"error": f"工作目录不存在: {workdir}"}

    logger.info("执行 %s: %s...", config["name"], prompt[:80])
    try:
        is_arg_style = config.get("prompt_style") == "arg"
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if not is_arg_style else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            if is_arg_style:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=AGENT_TIMEOUT
                )
            else:
                stdin_data = prompt.encode("utf-8")
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(stdin_data), timeout=AGENT_TIMEOUT
                )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"success": False, "output": f"[超时] 超过 {AGENT_TIMEOUT} 秒", "exit_code": -1}

        output = (stdout or b"").decode("utf-8", errors="replace")
        error = (stderr or b"").decode("utf-8", errors="replace")
        result = output if output else error
        logger.info("%s 完成 (%d chars, exit=%d)", config["name"], len(result), proc.returncode)
        return {
            "success": proc.returncode == 0,
            "output": result,
            "exit_code": proc.returncode,
            "agent": config["name"],
        }
    except FileNotFoundError:
        return {"success": False, "output": f"[错误] 找不到命令: {cmd[0]}", "exit_code": -1}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("执行 %s 失败", config["name"])
        return {"success": False, "output": f"[错误] {e}", "exit_code": -1}


def create_server(target: str) -> MCPServer:
    """工厂函数：创建 FastMCP server 实例"""
    mcp = MCPServer(f"mcp-bridge-{target}")
    config = AGENT_CONFIGS[target]

    @mcp.tool()
    async def execute_task(prompt: str, workdir: str | None = None) -> str:
        """在目标 AI 编码助手执行编码任务
        
        Args:
            prompt: 给编码助手的任务描述，需要清晰完整
            workdir: 工作目录路径（可选，默认当前目录）
        """
        result = await run_agent(target, prompt, workdir)
        return json.dumps(result, ensure_ascii=False, indent=2)

    logger.info("MCP bridge 已创建 → 目标: %s", config["name"])
    return mcp


# ─── 全局 FastMCP 实例（mcp run 需要发现全局 mcp 变量） ─────────

def _create_and_get_server() -> MCPServer:
    target = os.environ.get("MCP_BRIDGE_TARGET", "codex")
    if target not in AGENT_CONFIGS:
        print(f"错误: MCP_BRIDGE_TARGET={target} 无效", file=sys.stderr)
        sys.exit(1)
    return create_server(target)

mcp = _create_and_get_server()

if __name__ == "__main__":
    mcp.run()
