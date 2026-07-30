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
import re
import subprocess
import sys
import traceback
import urllib.request

from mcp.server import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] trio: %(message)s",
)
logger = logging.getLogger("trio-concerto")

# ─── 配置 ─────────────────────────────────────────────────────────

MAX_PROMPT_LEN = 8000
AGENT_TIMEOUT = 300      # OpenCode/Codex 超时
CC_TIMEOUT = 120         # CC 超时（已大幅改善，保留为熔断）
MAX_ITERATIONS = 3       # 最多修正 3 轮
GRAPHFIFY_TIMEOUT = 120

OPENSEVER_URL = "http://127.0.0.1:10100"

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


# ─── 基础工具 ─────────────────────────────────────────────────────

async def check_opencodex_health() -> bool:
    """检查 opencodex (:10100) 是否正常。CC/OpenCode/Codex 都依赖它。"""
    try:
        req = urllib.request.Request(
            f"{OPENSEVER_URL}/v1/messages",
            data=b'{"model":"deepseek/deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}',
            headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


async def run_agent(agent_id: str, prompt: str, workdir: str | None = None,
                    timeout: int | None = None) -> dict:
    """在指定 agent 上执行任务"""
    config = AGENTS.get(agent_id)
    if not config:
        return {"success": False, "output": f"未知 agent: {agent_id}"}

    cmd = list(config["cmd"]) + [prompt]
    cwd = workdir or os.getcwd()

    logger.info("> %s: %s...", config["name"], prompt[:80])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        timeout = timeout or AGENT_TIMEOUT
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"success": False, "output": f"[超时] {timeout}秒", "exit_code": -1}

        output = (stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")
        result = output if output else stderr_text

        # 清洗 Codex 输出（去掉基础设施日志和 prompt 回显）
        if agent_id == "codex":
            # 去掉头部: "Reading additional input from stdin..." 到 "--------\nuser\n"
            result = re.sub(
                r"Reading additional input from stdin.*?\n-{5,}\s*\nuser\s*\n",
                "", result, flags=re.DOTALL
            )
            # 去掉 prompt 回显: 所有直到 "codex\n" 的内容
            result = re.sub(r"^.*?\ncodex\s*\n", "", result, flags=re.DOTALL)
            # 去掉 "tokens used" 结尾
            result = re.sub(r"\ntokens used\s*\n.*?$", "", result, flags=re.DOTALL)
            # 去掉 warning 和分隔线
            result = re.sub(r"^warning:.*\n?", "", result, flags=re.MULTILINE)
            result = re.sub(r"^-{5,}\s*\n?", "", result)
            result = result.strip()

        # 清洗 CC 输出（去掉 CLAUDE.md 路由噪音和权限确认）
        if agent_id == "claude":
            result = re.sub(
                r"(?i)(please provide|what would you like|could you clarify|"
                r"i see you typed|based on your.*?md|"
                r"if your request matches|i need more context|"
                r"i.?ll help you.*?(execute|what)|"
                r"需要.*?确认|请确认|请提供).*?(\n|$)",
                "", result
            )
            result = result.strip()

        logger.info("< %s (%d chars, exit=%d)", config["name"], len(result), proc.returncode)
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
    logger.info("> graphify 图谱化 %s...", workdir)
    pipeline_log_entry = {"stage": "graphify 图谱化", "success": False, "output_preview": ""}

    for candidate_dir in ["graphify-out", ".graphify-out"]:
        full_path = os.path.join(workdir, candidate_dir)
        graph_json = os.path.join(full_path, "graph.json")
        if os.path.exists(graph_json):
            logger.info(" 发现已有图谱: %s", full_path)
            with open(graph_json) as f:
                graph_data = json.load(f)
            report_text = ""
            report_path = os.path.join(full_path, "GRAPH_REPORT.md")
            if os.path.exists(report_path):
                with open(report_path) as f:
                    report_text = f.read()
            preview = f"{len(graph_data.get('nodes', []))} nodes / {len(graph_data.get('edges', []))} edges"
            pipeline_log_entry["success"] = True
            pipeline_log_entry["output_preview"] = preview
            return {"success": True, "output": "", "graph": graph_data,
                    "report": report_text, "graph_dir": full_path,
                    "pipeline_log_entry": pipeline_log_entry}

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
        graph_path = os.path.join(out_dir, "graphify-out", "graph.json")
        report_path = os.path.join(out_dir, "graphify-out", "GRAPH_REPORT.md")
        graph_data = None
        report_text = ""
        if os.path.exists(graph_path):
            with open(graph_path) as f:
                graph_data = json.load(f)
        if os.path.exists(report_path):
            with open(report_path) as f:
                report_text = f.read()

        preview = f"{len(graph_data.get('nodes', [])) if graph_data else 0} nodes / {len(graph_data.get('edges', [])) if graph_data else 0} edges"
        pipeline_log_entry["success"] = True
        pipeline_log_entry["output_preview"] = preview
        logger.info("graphify 完成: %s", preview)
        return {"success": True, "output": log_text, "graph": graph_data,
                "report": report_text, "graph_dir": out_dir,
                "pipeline_log_entry": pipeline_log_entry}

    except FileNotFoundError:
        logger.warning("graphify 命令不存在，跳过")
        pipeline_log_entry["output_preview"] = "graphify 未安装，跳过"
        return {"success": False, "output": "graphify 未安装", "graph": None,
                "pipeline_log_entry": pipeline_log_entry}
    except Exception as e:
        logger.exception("graphify 失败")
        pipeline_log_entry["output_preview"] = str(e)[:80]
        return {"success": False, "output": str(e), "graph": None,
                "pipeline_log_entry": pipeline_log_entry}


# ─── 流水线 ───────────────────────────────────────────────────────

async def trio_pipeline(task: str, workdir: str | None = None, graphify: bool = False) -> dict:
    """三体协奏流水线: CC -> OpenCode -> Codex (循环修正)"""
    logger.info("=" * 50)
    logger.info("三体协奏启动")
    logger.info("任务: %s", task[:100])
    logger.info("=" * 50)

    pipeline_log = []
    best_code_output = ""
    review_feedback = ""
    graph_context = ""

    # ─── Stage 0: graphify（可选） ────────────────────────────
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
            community_lines = [
                f"  - {n.get('label', n.get('id', '?'))}"
                for n in gr["graph"].get("nodes", [])[:30]
            ]
            graph_context = (
                f"\n{'='*40}\n"
                f"## 项目知识图谱 ({nodes} 节点, {edges} 边)\n"
                f"核心节点:\n" + "\n".join(community_lines[:15]) + "\n"
            )
            if report:
                summary_match = re.search(r"## Summary.*?(?=\n##)", report, re.DOTALL)
                if summary_match:
                    graph_context += f"\n图谱摘要:\n{summary_match.group()[:500]}\n"
                god_match = re.search(r"## God Nodes.*?(?=\n##)", report, re.DOTALL)
                if god_match:
                    graph_context += f"\n{god_match.group()[:500]}\n"
            graph_context += f"{'='*40}\n"

    # ─── 检查 opencodex ──────────────────────────────────────
    opencodex_ok = await check_opencodex_health()
    pipeline_log.append({
        "stage": "opencodex 检查",
        "success": opencodex_ok,
        "output_preview": "已就绪 (:10100)" if opencodex_ok else "未运行!",
    })
    if not opencodex_ok:
        logger.warning("opencodex (:10100) 未运行！")

    # ─── Stage 1: CC 思考分析 ────────────────────────────────
    logger.info("[Stage 1/3] CC 思考分析...")
    think_prompt = (
        "分析以下任务，输出实施方案。不要提你的 skill 或工具，直接给出技术方案。\n\n"
        f"任务: {task}\n\n"
        f"{graph_context}"
        "## 需求分析\n"
        "## 架构方案\n"
        "## 实施步骤\n"
        "## 技术要点"
    )
    think_result = await run_agent("claude", think_prompt, workdir, timeout=CC_TIMEOUT)
    plan = think_result.get("output", "无方案输出")
    cc_success = think_result.get("success", False)

    # 【熔断】CC 超时/失败时 OpenCode 兜底
    if not cc_success or len(plan.strip()) < 50:
        logger.warning("CC 阶段失败 (%s)，OpenCode 兜底...",
                       "超时" if "超时" in plan else plan[:50])
        fallback_prompt = (
            "你是一个架构分析师。请为以下任务输出技术实施方案。\n\n"
            f"任务: {task}\n\n"
            f"{graph_context}"
            "直接输出：\n"
            "## 需求分析\n"
            "## 架构方案\n"
            "## 实施步骤\n"
            "## 技术要点"
        )
        fallback_result = await run_agent("opencode", fallback_prompt, workdir)
        fallback_plan = fallback_result.get("output", "")
        if fallback_plan and len(fallback_plan.strip()) > 50:
            plan = fallback_plan
            logger.info("OpenCode 兜底成功 (%d chars)", len(fallback_plan))
        else:
            logger.warning("OpenCode 兜底也失败")

    pipeline_log.append({
        "stage": "CC 思考分析",
        "success": cc_success,
        "output_preview": plan[:200],
    })

    code_path = os.path.join(workdir or "/tmp", ".trio_code_output.py")

    for iteration in range(MAX_ITERATIONS):
        iter_label = f"第{iteration + 1}轮"
        logger.info("-" * 40)
        logger.info("[Stage 2/3] %s OpenCode...", iter_label)

        prev_code_snippet = best_code_output[:2000] if iteration > 0 and best_code_output else ""

        # ─── Stage 2: OpenCode 编码 ──────────────────────────
        if iteration > 0 and prev_code_snippet:
            code_prompt = (
                "根据以下方案、任务和审核反馈，修改代码。\n\n"
                f"{graph_context}"
                f"## 实施方案\n{plan}\n"
                f"## 任务\n{task}\n"
                f"\n## 上一轮生成的代码\n"
                f"```python\n{prev_code_snippet}\n```\n"
                f"\n## 上一轮审核反馈\n"
                f"{review_feedback}\n\n"
                "请在上一轮代码基础上修改。\n"
            )
        else:
            code_prompt = (
                "根据以下方案和任务进行编码实现或代码修复。\n\n"
                f"{graph_context}"
                f"## 实施方案\n{plan}\n"
                f"## 任务\n{task}\n"
                "\n如果是审查/评审类任务，请直接修改项目源文件修复发现的问题；"
                "如果是编码类任务，请输出最终代码（用 markdown 代码块包裹）。\n"
            )

        code_result = await run_agent("opencode", code_prompt, workdir)
        code_output = code_result.get("output", "无代码输出")
        best_code_output = code_output

        try:
            code_match = re.search(r"```(?:python)?\s*\n(.*?)\n```", code_output, re.DOTALL)
            clean_code = code_match.group(1) if code_match else code_output
            with open(code_path, "w") as f:
                f.write(clean_code)
        except Exception:
            pass

        pipeline_log.append({
            "stage": f"OpenCode 编码 ({iter_label})",
            "success": code_result.get("success", False),
            "output_preview": code_output[:200],
        })

        # ─── Stage 3: Codex 审核 ────────────────────────────
        logger.info("[Stage 3/3] %s Codex 审核...", iter_label)
        review_prompt = (
            "你是一个代码审查员。严格审查以下代码，找出所有问题。\n\n"
            f"{'=' * 40}\n"
            f"## 原始任务\n{task}\n\n"
            f"## 实施方案\n{plan[:3000]}\n\n"
            f"## 实现代码\n"
            f"```python\n{best_code_output[:8000]}\n```\n"
            f"{'=' * 40}\n\n"
            "请按以下格式输出:\n"
            "## 通过状态: [通过/有条件通过/不通过]\n"
            "## 发现的问题\n"
            "## 改进建议"
        )
        review_result = await run_agent("codex", review_prompt, workdir)
        review_feedback = review_result.get("output", "无审核意见")

        pipeline_log.append({
            "stage": f"Codex 审核 ({iter_label})",
            "success": review_result.get("success", False),
            "output_preview": review_feedback[:200],
        })

        # ─── 判定：全文搜索而非前 N 字符 ─────────────────────
        is_passed = bool(re.search(r"## 通过状态:\s*(通过|有条件通过)", review_feedback))

        if is_passed:
            logger.info("审核通过，流水线完成")
            pipeline_log.append({
                "stage": "结论",
                "success": True,
                "output_preview": f"审核通过，共{iteration + 1}轮",
            })
            break
        else:
            remaining = MAX_ITERATIONS - iteration - 1
            if remaining > 0:
                logger.info("审核未通过（第%d轮），继续修正", iteration + 1)
            else:
                logger.info("已达最大修正次数(%d)", MAX_ITERATIONS)
                pipeline_log.append({
                    "stage": "结论",
                    "success": False,
                    "output_preview": f"已达最大修正次数({MAX_ITERATIONS})",
                })

    # ─── 清理 ─────────────────────────────────────────────
    try:
        if os.path.exists(code_path):
            os.remove(code_path)
    except Exception:
        pass

    summary = {
        "task": task,
        "plan_preview": plan[:500],
        "code_preview": best_code_output[:1000],
        "review_preview": review_feedback[:500] if review_feedback else "无审核",
        "pipeline_log": pipeline_log,
        "total_stages": len(pipeline_log),
    }
    return summary


# ─── MCP Server ─────────────────────────────────────────────────

mcp = MCPServer("trio-concerto")


@mcp.tool()
async def trio_concerto(task: str, workdir: str | None = None, graphify: bool = False) -> str:
    """三体协奏 — 三 agent 流水线执行任务

    Hermes 调度 -> CC(思考分析) -> OpenCode(编码执行/修复) -> Codex(审核验证)

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
        cmd_name = config["cmd"][0]
        cmd_check = subprocess.run(["which", cmd_name], capture_output=True, text=True)
        status = "OK" if cmd_check.returncode == 0 else "MISSING"
        lines.append(f"- {config['name']} ({aid}) [{status}]")
        lines.append(f"  职责: {config['desc']}")
        lines.append(f"  命令: {' '.join(config['cmd'][:2])}...")
        lines.append("")

    try:
        req = urllib.request.Request(f"{OPENSEVER_URL}/v1/messages")
        urllib.request.urlopen(req, timeout=2)
        lines.append("- opencodex (:10100) [RUNNING]")
    except Exception:
        lines.append("- opencodex (:10100) [DOWN]")

    lines.append("")
    lines.append("### 流水线")
    lines.append("0. [可选] graphify 图谱化")
    lines.append("1. CC 思考分析 -> 方案")
    lines.append("2. OpenCode 编码实现 -> 代码")
    lines.append("3. Codex 审核验证 -> 意见")
    lines.append("4. 不合格则循环修正（最多3轮）")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
