#!/usr/bin/env python3
"""
三体协奏 — 三 agent 流水线编排
Hermes 调度 → CC 思考分析 → OpenCode 编码执行 → Codex 审核验证

架构:
  用户任务 → CC(分析/规划) → OpenCode(编码实现) → Codex(审核验证) → 最终输出
                                         ↑  不合格循环  ↓
                                        └── 反馈修正 ──┘

注册: hermes mcp add trio-concerto --command mcp --args run /home/user/trio-concerto.py
"""

import asyncio
import json
import logging
import os
import sys
import traceback

from mcp.server import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] trio: %(message)s",
)
logger = logging.getLogger("trio-concerto")

LITELLM_PORT = 53684
LITELLM_CONFIG = os.path.expanduser("~/.hermes/litellm-config.yaml")
_litellm_proc: asyncio.subprocess.Process | None = None


async def ensure_litellm() -> bool:
    """Check if LiteLLM is running on :53684, start if not. CC 依赖它做协议转换。"""
    global _litellm_proc
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{LITELLM_PORT}/health")
        urllib.request.urlopen(req, timeout=2)
        logger.info("LiteLLM 已在运行 (:53684)")
        return True
    except Exception:
        logger.info("LiteLLM 未运行，正在启动...")
    if not os.path.exists(LITELLM_CONFIG):
        logger.error("LiteLLM 配置文件不存在: %s", LITELLM_CONFIG)
        return False
    try:
        _litellm_proc = await asyncio.create_subprocess_exec(
            "litellm", "--config", LITELLM_CONFIG, "--port", str(LITELLM_PORT),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # 等待启动
        for i in range(10):
            await asyncio.sleep(2)
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{LITELLM_PORT}/health")
                urllib.request.urlopen(req, timeout=2)
                logger.info("LiteLLM 启动成功")
                return True
            except Exception:
                continue
        logger.error("LiteLLM 启动超时")
        return False
    except Exception as e:
        logger.error("LiteLLM 启动失败: %s", e)
        return False


async def stop_litellm():
    """停止本次启动的 LiteLLM（如果是本进程启动的）"""
    global _litellm_proc
    if _litellm_proc:
        _litellm_proc.kill()
        await _litellm_proc.wait()
        _litellm_proc = None
        logger.info("LiteLLM 已停止")


MAX_PROMPT_LEN = 8000
AGENT_TIMEOUT = 300  # 5 min per stage
MAX_ITERATIONS = 3   # 最多循环修正 3 轮
GRAPHFIFY_TIMEOUT = 120  # graphify 超时秒数

AGENTS = {
    "claude": {
        "name": "Claude Code",
        "cmd": ["claude", "-p"],
        "desc": "思考分析、架构规划",
    },
    "opencode": {
        "name": "OpenCode",
        "cmd": ["opencode", "run", "--pure", "-m", "deepseek-direct/deepseek-v4-flash"],
        "desc": "编码执行、多文件编辑",
    },
    "codex": {
        "name": "Codex",
        "cmd": ["codex", "exec", "--model", "deepseek-v4-flash",
                "-s", "danger-full-access",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check"],
        "desc": "审核验证、代码审查",
    },
}


async def run_agent(agent_id: str, prompt: str, workdir: str | None = None) -> dict:
    """在指定 agent 上执行任务"""
    config = AGENTS.get(agent_id)
    if not config:
        return {"success": False, "output": f"未知 agent: {agent_id}"}

    cmd = list(config["cmd"]) + [prompt]
    cwd = workdir or os.getcwd()

    logger.info("→ %s: %s...", config["name"], prompt[:80])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=AGENT_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"success": False, "output": f"[超时] {AGENT_TIMEOUT}秒", "exit_code": -1}

        output = (stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")
        result = output if output else stderr_text

        # 对 Codex 输出做清洗（去掉基础设施日志）
        if agent_id == "codex":
            import re
            result = re.sub(
                r"Reading additional input from stdin.*?--------\n(?:workdir:.*?\n)*"
                r"(?:model:.*?\n)*(?:provider:.*?\n)*(?:approval:.*?\n)*(?:sandbox:.*?\n)*"
                r"(?:reasoning.*?\n)*(?:session.*?\n)*",
                "", result, flags=re.DOTALL
            )
            result = re.sub(r"^warning:.*\n?", "", result, flags=re.MULTILINE)
            result = re.sub(r"\ntokens used(\n.*)?$", "", result, flags=re.DOTALL)
            result = result.strip()
        # 对 CC 输出做清洗（去掉 CLAUDE.md 路由噪音）
        if agent_id == "claude":
            import re
            # 去掉"请提供更多详情"类问句
            result = re.sub(
                r"(?i)(please provide|what would you like|could you clarify|"
                r"i see you typed|based on your.*?md|"
                r"if your request matches|i need more context|"
                r"i'll help you.*?(execute|what))"
                r".*?(\n|$)",
                "", result
            )
            result = result.strip()

        logger.info("← %s (%d chars, exit=%d)", config["name"], len(result), proc.returncode)
        return {
            "success": proc.returncode == 0,
            "output": result,
            "agent": config["name"],
        }
    except FileNotFoundError:
        return {"success": False, "output": f"命令不存在: {cmd[0]}", "agent": config["name"]}
    except Exception as e:
        return {"success": False, "output": f"[错误] {e}", "agent": config["name"]}


async def run_graphify(workdir: str) -> dict:
    """在 workdir 上运行 graphify 构建知识图谱，返回图谱报告"""
    logger.info("▶️ graphify 图谱化 %s...", workdir)
    pipeline_log_entry = {"stage": "graphify 图谱化", "success": False, "output_preview": ""}

    # 优先检测已有的 graph（兼容 graphify-out/ 和 .graphify-out/ 两种路径）
    for candidate_dir in ["graphify-out", ".graphify-out"]:
        full_path = os.path.join(workdir, candidate_dir)
        graph_json = os.path.join(full_path, "graph.json")
        if os.path.exists(graph_json):
            logger.info(" 发现已有图谱: %s", full_path)
            graph_data = json.load(open(graph_json))
            report_text = ""
            report_path = os.path.join(full_path, "GRAPH_REPORT.md")
            if os.path.exists(report_path):
                report_text = open(report_path).read()
            preview = f"{len(graph_data.get('nodes', []))} nodes / {len(graph_data.get('edges', []))} edges"
            pipeline_log_entry["success"] = True
            pipeline_log_entry["output_preview"] = preview
            return {"success": True, "output": "", "graph": graph_data,
                    "report": report_text, "graph_dir": full_path,
                    "pipeline_log_entry": pipeline_log_entry}

    # 不存在已有图谱，重新构建
    out_dir = os.path.join(workdir, ".graphify-out")
    cmd = ["graphify", workdir, "--output", out_dir]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=GRAPHFIFY_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"success": False, "output": "[超时] graphify 超时", "graph": None,
                    "pipeline_log_entry": pipeline_log_entry}

        log_text = (stdout or b"").decode("utf-8", errors="replace")
        # 解析 graph.json
        graph_path = os.path.join(out_dir, "graphify-out", "graph.json")
        report_path = os.path.join(out_dir, "graphify-out", "GRAPH_REPORT.md")
        graph_data = None
        report_text = ""
        if os.path.exists(graph_path):
            with open(graph_path) as f:
                graph_data = json.load(f)
            logger.info("  graph.json: %d nodes, %d edges",
                        len(graph_data.get("nodes", [])),
                        len(graph_data.get("edges", [])))
        if os.path.exists(report_path):
            with open(report_path) as f:
                report_text = f.read()

        preview = f"{len(graph_data.get('nodes', [])) if graph_data else 0} nodes / {len(graph_data.get('edges', [])) if graph_data else 0} edges"
        pipeline_log_entry["success"] = True
        pipeline_log_entry["output_preview"] = preview
        logger.info("✅ graphify 完成: %s", preview)
        return {"success": True, "output": log_text, "graph": graph_data,
                "report": report_text, "graph_dir": out_dir,
                "pipeline_log_entry": pipeline_log_entry}

    except FileNotFoundError:
        logger.warning("graphify 命令不存在，跳过图谱化")
        pipeline_log_entry["output_preview"] = "graphify 未安装，跳过"
        return {"success": False, "output": "graphify 未安装", "graph": None,
                "pipeline_log_entry": pipeline_log_entry}
    except Exception as e:
        logger.exception("graphify 失败")
        pipeline_log_entry["output_preview"] = str(e)[:80]
        return {"success": False, "output": str(e), "graph": None,
                "pipeline_log_entry": pipeline_log_entry}


async def trio_pipeline(task: str, workdir: str | None = None, graphify: bool = False) -> dict:
    """
    三体协奏流水线:
    1. CC 思考分析 → 输出方案
    2. OpenCode 编码实现 → 输出代码（review_only=True 时跳过）
    3. Codex 审核验证 → 输出审核意见
    4. 如有问题则循环修正
    """
    logger.info("=" * 50)
    logger.info("三体协奏启动")
    logger.info(f"任务: {task[:100]}")
    logger.info("=" * 50)

    pipeline_log = []
    best_code_output = ""
    graph_context = ""

    # ─── Stage 0: graphify 图谱化（可选） ─────────────────────────
    if graphify and workdir:
        logger.info("[Stage 0/3] graphify 图谱化...")
        gr = await run_graphify(workdir)
        pipeline_log.append(gr.get("pipeline_log_entry", {
            "stage": "graphify 图谱化", "success": False, "output_preview": "跳过"
        }))
        if gr.get("graph"):
            nodes = len(gr["graph"].get("nodes", []))
            edges = len(gr["graph"].get("edges", []))
            report = gr.get("report", "")
            # 构建给后续 agent 的图谱上下文
            community_lines = []
            for n in gr["graph"].get("nodes", [])[:30]:
                community_lines.append(f"  - {n.get('label', n.get('id', '?'))}")
            graph_context = (
                f"\n{'='*40}\n"
                f"## 项目知识图谱（{nodes} 节点, {edges} 边）\n"
                f"核心节点:\n" + "\n".join(community_lines[:15]) + "\n"
            )
            if report:
                # 提取关键部分
                import re
                summary_match = re.search(r"## Summary.*?(?=\n##)", report, re.DOTALL)
                if summary_match:
                    graph_context += f"\n图谱摘要:\n{summary_match.group()[:500]}\n"
                god_match = re.search(r"## God Nodes.*?(?=\n##)", report, re.DOTALL)
                if god_match:
                    graph_context += f"\n{god_match.group()[:500]}\n"
            graph_context += f"{'='*40}\n"
            logger.info("Stage 0 完成: %d nodes, %d edges", nodes, edges)
        else:
            logger.info("Stage 0: graphify 无输出，跳过")

    # ─── LiteLLM 确保运行（CC 依赖） ────────────────────────────
    if not await ensure_litellm():
        logger.warning("LiteLLM 未运行，CC 阶段可能失败")
        pipeline_log.append({
            "stage": "LiteLLM 检查", "success": False, "output_preview": "LiteLLM 未运行"
        })
    else:
        pipeline_log.append({
            "stage": "LiteLLM 检查", "success": True, "output_preview": "已就绪"
        })

    # ─── Stage 1: CC 思考分析 ────────────────────────────────────
    logger.info("[Stage 1/3] CC 思考分析...")
    think_prompt = (
        f"分析以下任务，输出实施方案。不要提你的skill或工具，直接给出技术方案。\n\n"
        f"任务: {task}\n\n"
        f"{graph_context}"
        f"## 需求分析\n"
        f"## 架构方案\n"
        f"## 实施步骤\n"
        f"## 技术要点"
    )
    think_result = await run_agent("claude", think_prompt, workdir)
    plan = think_result.get("output", "无方案输出")
    pipeline_log.append({
        "stage": "CC 思考分析",
        "success": think_result.get("success", False),
        "output_preview": plan[:200],
    })
    logger.info("Stage 1 完成: %s", "✅" if think_result.get("success") else "⚠️")

    for iteration in range(MAX_ITERATIONS):
        iter_label = f"第{iteration + 1}轮"
        logger.info("─" * 40)
        logger.info("[Stage 2/3] %s OpenCode 编码执行...", iter_label)

        # 把代码落地到临时文件，供后续 agent 读取
        code_path = os.path.join(workdir or "/tmp", ".trio_code_output.py")
        if iteration > 0 and best_code_output:
            prev_code_snippet = best_code_output[:2000]
        else:
            prev_code_snippet = ""

        # ─── Stage 2: OpenCode 编码实现/修复 ──────────────────────
        # 根据任务类型和修正轮次自动调整 prompt
        if iteration > 0 and prev_code_snippet:
            # 修正轮：基于上一轮代码 + 审核反馈做增量修改
            code_prompt = (
                f"根据以下方案、任务和审核反馈，修改代码。\n\n"
                f"{graph_context}"
                f"## 实施方案\n{plan}\n"
                f"## 任务\n{task}\n"
                f"\n## 上一轮生成的代码\n"
                f"```python\n{prev_code_snippet}\n```\n"
                f"\n## 上一轮审核反馈\n"
                f"{review_feedback}\n\n"
                f"请在上一轮代码基础上修改。如果是审查类任务，请直接修改项目中的源文件；"
                f"如果是编码类任务，请输出最终代码。\n"
            )
        else:
            # 首轮：根据方案从零开始
            code_prompt = (
                f"根据以下方案和任务进行编码实现或代码修复。\n\n"
                f"{graph_context}"
                f"## 实施方案\n{plan}\n"
                f"## 任务\n{task}\n"
                f"\n如果是审查/评审类任务，请直接修改项目源文件修复发现的问题；"
                f"如果是编码类任务，请输出最终代码（用 markdown 代码块包裹）。\n"
            )

        code_result = await run_agent("opencode", code_prompt, workdir)
        code_output = code_result.get("output", "无代码输出")
        best_code_output = code_output

        # 将代码写入临时文件，供后续审核和修正使用
        code_path = os.path.join(workdir or "/tmp", ".trio_code_output.py")
        try:
            # 从 markdown 代码块中提取纯代码
            import re
            code_match = re.search(r"```(?:python)?\s*\n(.*?)\n```", code_output, re.DOTALL)
            clean_code = code_match.group(1) if code_match else code_output
            with open(code_path, "w") as f:
                f.write(clean_code)
            logger.info("代码已写入 %s (%d bytes)", code_path, len(clean_code))
        except Exception as e:
            logger.warning("写入代码文件失败: %s", e)

        pipeline_log.append({
            "stage": f"OpenCode 编码 ({iter_label})",
            "success": code_result.get("success", False),
            "output_preview": code_output[:200],
        })
        logger.info("Stage 2 完成")

        # ─── Stage 3: Codex 审核验证 ─────────────────────────────
        logger.info("[Stage 3/3] %s Codex 审核验证...", iter_label)
        code_path = os.path.join(workdir or "/tmp", ".trio_code_output.py")
        review_prompt = (
            f"你是一个代码审查员。严格审查以下代码/方案，找出所有问题。\n\n"
            f"{'=' * 40}\n"
            f"## 原始任务\n{task}\n\n"
            f"## 实施方案\n{plan[:3000]}\n\n"
            f"## 实现代码（已写入 {code_path}）\n"
            f"```python\n{best_code_output[:8000]}\n```\n"
            f"{'=' * 40}\n\n"
            f"请按以下格式输出:\n"
            f"## 通过状态: [通过/有条件通过/不通过]\n"
            f"## 发现的问题\n"
            f"## 改进建议"
        )
        review_result = await run_agent("codex", review_prompt, workdir)
        review_feedback = review_result.get("output", "无审核意见")

        pipeline_log.append({
            "stage": f"Codex 审核 ({iter_label})",
            "success": review_result.get("success", False),
            "output_preview": review_feedback[:200],
        })
        logger.info("Stage 3 完成")

        # ─── 判断是否需要继续修正 ────────────────────────────────
        review_text = review_feedback[:500]  # 扫前 500 字符判断
        is_passed = "通过" in review_text and "不通过" not in review_text
        # "有条件通过" 算通过
        if "有条件通过" in review_text:
            is_passed = True

        if is_passed:
            logger.info("✅ 审核通过，流水线完成")
            pipeline_log.append({
                "stage": "结论",
                "success": True,
                "output_preview": f"审核通过，共{iteration + 1}轮",
            })
            break
        else:
            logger.info("⚠️ 审核未通过，第%s轮修正", iteration + 2 if iteration + 1 < MAX_ITERATIONS else "已达上限")
            if iteration + 1 >= MAX_ITERATIONS:
                pipeline_log.append({
                    "stage": "结论",
                    "success": False,
                    "output_preview": f"已达最大修正次数({MAX_ITERATIONS})，输出最终版本",
                })
    else:
        logger.info("⚠️ 已达最大修正次数，输出最终版本")

    # ─── 清理临时文件 ─────────────────────────────────────────────
    code_path = os.path.join(workdir or "/tmp", ".trio_code_output.py")
    try:
        if os.path.exists(code_path):
            os.remove(code_path)
    except Exception:
        pass

    # ─── 汇总输出 ────────────────────────────────────────────────
    summary = {
        "task": task,
        "plan_preview": plan[:500],
        "code_preview": best_code_output[:1000],
        "review_preview": review_feedback[:500] if 'review_feedback' in dir() else "无审核",
        "pipeline_log": pipeline_log,
        "total_stages": len(pipeline_log),
    }
    return summary


# ─── MCP Server ─────────────────────────────────────────────────

mcp = MCPServer("trio-concerto")


@mcp.tool()
async def trio_concerto(task: str, workdir: str | None = None, graphify: bool = False) -> str:
    """三体协奏 — 三 agent 流水线执行任务

    Hermes 调度 → CC(思考分析) → OpenCode(编码执行/修复) → Codex(审核验证)

    Args:
        task: 任务描述，需要清晰完整的需求说明
        workdir: 工作目录路径（可选）
        graphify: 是否先运行 graphify 构建项目知识图谱（需要 workdir）
    """
    if len(task) > MAX_PROMPT_LEN:
        return json.dumps({"error": f"任务过长: {len(task)} > {MAX_PROMPT_LEN}"})

    if graphify and not workdir:
        return json.dumps({"error": "graphify=True 时需要指定 workdir"})

    result = await trio_pipeline(task, workdir, graphify=graphify)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def trio_status() -> str:
    """查看三体协奏各 agent 状态"""
    lines = ["## 三体协奏 Agent 状态\n"]
    for aid, config in AGENTS.items():
        lines.append(f"- **{config['name']}** (`{aid}`)")
        lines.append(f"  - 职责: {config['desc']}")
        lines.append(f"  - 命令: `{' '.join(config['cmd'][:2])}...`")
        lines.append("")
    lines.append("### 流水线")
    lines.append("0. [可选] graphify 图谱化 → 项目知识图谱")
    lines.append("1. CC 思考分析 → 方案")
    lines.append("2. OpenCode 编码实现 → 代码")
    lines.append("3. Codex 审核验证 → 意见")
    lines.append("4. 不合格则循环修正（最多3轮）")
    lines.append("")
    lines.append("### 调用示例")
    lines.append("```python")
    lines.append("# 不带图谱（快速模式）")
    lines.append('trio_concerto(task="实现一个函数", workdir="/path")')
    lines.append("")
    lines.append("# 带graphify图谱（推荐用于代码库项目）")
    lines.append('trio_concerto(task="实现一个函数", workdir="/path", graphify=True)')
    lines.append("```")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
