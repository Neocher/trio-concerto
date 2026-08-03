#!/usr/bin/env python3
"""
三体协奏 v2 — MCP 编排适配层
============================
Hermes 调用入口（MCP stdio）→ acp_bridge (:8770) HTTP 编排三阶段流水线：
  CC 思考分析 → OpenCode 编码实现 → Codex 审核验证（不合格循环修正，最多 3 轮）

底层复用 v2 全部韧性（双超时 900s/120-180s、流式心跳、熔断半开、内存防护、
进程组击杀），本层只负责三阶段编排与任务跟踪。

注册: hermes mcp add trio-concerto --command mcp --args run /home/user/trio-concerto.py
环境变量: TRIO_BRIDGE (默认 http://127.0.0.1:8770)
"""
import asyncio
import json
import os
import re
import time
import uuid

import httpx
from mcp.server import MCPServer

BRIDGE = os.environ.get("TRIO_BRIDGE", "http://127.0.0.1:8770")
AGENT_CC = "claude-code"
AGENT_OC = "opencode"
AGENT_CX = "codex"
MAX_ITERATIONS = 3
POLL_INTERVAL = 2.0
STAGE_TIMEOUT = 950          # 单阶段等待上限（v2 自身 total 900s，编排层留缓冲）
MAX_PROMPT_LEN = 8000

# ─── 编排任务注册表（进程内；MCP stdio 服务器由 Hermes watchdog 长驻）───
_tasks: dict[str, dict] = {}


def _new_task(task: str, workdir: str | None, graphify: bool) -> str:
    task_id = uuid.uuid4().hex[:8]
    _tasks[task_id] = {
        "task_id": task_id,
        "task": task[:120],
        "workdir": workdir,
        "graphify": graphify,
        "status": "queued",          # queued -> running -> done | error
        "progress": [],
        "stages": [],                # 各阶段 agent 任务明细
        "created_at": time.time(),
        "updated_at": time.time(),
        "result": None,
        "error": None,
    }
    return task_id


def _update_task(task_id: str, **kw) -> None:
    entry = _tasks.get(task_id)
    if entry:
        entry.update(kw)
        entry["updated_at"] = time.time()


async def _dispatch(client: httpx.AsyncClient, agent: str, prompt: str) -> str:
    """派发单 agent 任务，返回 v2 task_id"""
    r = await client.post(f"{BRIDGE}/dispatch",
                          json={"target_agent": agent, "prompt": prompt})
    r.raise_for_status()
    return r.json()["task_id"]


async def _wait_task(client: httpx.AsyncClient, agent_task_id: str,
                     timeout: int = STAGE_TIMEOUT) -> dict:
    """轮询 v2 任务直至 completed/failed，返回任务 dict"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = await client.get(f"{BRIDGE}/tasks/{agent_task_id}")
        t = r.json()
        if t["status"] in ("completed", "failed"):
            return t
        await asyncio.sleep(POLL_INTERVAL)
    return {"status": "failed", "reason": "orchestrator_timeout",
            "output": "", "error": "orchestrator wait timeout"}


def _load_graph_context(workdir: str) -> str:
    """复用已有 graphify 产物（不重新生成），注入图谱摘要"""
    for cand in ("graphify-out/graph.json", ".graphify-out/graph.json"):
        p = os.path.join(workdir, cand)
        if os.path.exists(p):
            try:
                g = json.load(open(p))
                nodes = len(g.get("nodes", []))
                edges = len(g.get("edges", []))
                return (f"\n{'='*40}\n## 项目知识图谱 ({nodes} 节点, {edges} 边)\n"
                        f"（graphify-out 产物，仅摘要注入）\n{'='*40}\n")
            except Exception:
                return ""
    return ""


async def _run_orchestration(task_id: str, task: str,
                             workdir: str | None, graphify: bool) -> None:
    """后台执行三阶段编排，进度逐 stage 写入注册表"""

    def report(stage: str, success: bool, preview: str = "") -> None:
        _update_task(task_id, progress=[*_tasks[task_id]["progress"], {
            "stage": stage, "success": success, "preview": preview[:200],
            "ts": time.time(),
        }])

    _update_task(task_id, status="running")
    try:
        graph_context = _load_graph_context(workdir) if (graphify and workdir) else ""
        async with httpx.AsyncClient(base_url=BRIDGE, timeout=30,
                                     trust_env=False) as client:
            # ─── Stage 1: CC 思考分析 ────────────────────────────
            think_prompt = (
                "分析以下任务，输出实施方案。不要提你的 skill 或工具，直接给出技术方案。\n\n"
                f"任务: {task}\n\n{graph_context}"
                "## 需求分析\n## 架构方案\n## 实施步骤\n## 技术要点"
            )
            cc_tid = await _dispatch(client, AGENT_CC, think_prompt)
            cc_task = await _wait_task(client, cc_tid)
            plan = cc_task.get("output", "")
            cc_ok = cc_task["status"] == "completed"
            _update_task(task_id, stages=[*_tasks[task_id]["stages"],
                                          {"agent": "CC 思考分析", "task_id": cc_tid,
                                           "status": cc_task["status"],
                                           "elapsed": cc_task.get("elapsed")}])

            # 【熔断】CC 超时/失败 → OpenCode 兜底
            if not cc_ok or len(plan.strip()) < 50:
                fb_prompt = (
                    "你是一个架构分析师。请为以下任务输出技术实施方案。\n\n"
                    f"任务: {task}\n\n{graph_context}"
                    "直接输出：\n## 需求分析\n## 架构方案\n## 实施步骤\n## 技术要点"
                )
                fb_tid = await _dispatch(client, AGENT_OC, fb_prompt)
                fb_task = await _wait_task(client, fb_tid)
                fb_plan = fb_task.get("output", "")
                if fb_task["status"] == "completed" and len(fb_plan.strip()) > 50:
                    plan = fb_plan
                else:
                    plan = plan or "（无方案输出）"
            report("CC 思考分析", cc_ok, plan[:200])

            # ─── Stage 2+3: OpenCode 编码 → Codex 审核（循环修正）──
            best_code = ""
            review = ""
            passed = False
            for iteration in range(MAX_ITERATIONS):
                label = f"第{iteration + 1}轮"
                if iteration > 0 and best_code:
                    code_prompt = (
                        "根据以下方案、任务和审核反馈，修改代码。\n\n"
                        f"{graph_context}## 实施方案\n{plan}\n## 任务\n{task}\n"
                        f"\n## 上一轮生成的代码\n```python\n{best_code[:2000]}\n```\n"
                        f"\n## 上一轮审核反馈\n{review}\n\n请在上一轮代码基础上修改。\n"
                    )
                else:
                    code_prompt = (
                        "根据以下方案和任务进行编码实现或代码修复。\n\n"
                        f"{graph_context}## 实施方案\n{plan}\n## 任务\n{task}\n"
                        "\n如果是审查/评审类任务，请直接修改项目源文件修复发现的问题；"
                        "如果是编码类任务，请输出最终代码（用 markdown 代码块包裹）。\n"
                    )
                oc_tid = await _dispatch(client, AGENT_OC, code_prompt)
                oc_task = await _wait_task(client, oc_tid)
                best_code = oc_task.get("output", "")
                oc_ok = oc_task["status"] == "completed"
                _update_task(task_id, stages=[*_tasks[task_id]["stages"],
                                              {"agent": f"OpenCode 编码 ({label})",
                                               "task_id": oc_tid,
                                               "status": oc_task["status"],
                                               "elapsed": oc_task.get("elapsed")}])
                report(f"OpenCode 编码 ({label})", oc_ok, best_code[:200])

                review_prompt = (
                    "你是一个代码审查员。严格审查以下代码，找出所有问题。\n\n"
                    f"{'=' * 40}\n## 原始任务\n{task}\n\n"
                    f"## 实施方案\n{plan[:3000]}\n\n"
                    f"## 实现代码\n```python\n{best_code[:8000]}\n```\n{'=' * 40}\n\n"
                    "请按以下格式输出:\n## 通过状态: [通过/有条件通过/不通过]\n"
                    "## 发现的问题\n## 改进建议"
                )
                cx_tid = await _dispatch(client, AGENT_CX, review_prompt)
                cx_task = await _wait_task(client, cx_tid)
                review = cx_task.get("output", "")
                cx_ok = cx_task["status"] == "completed"
                _update_task(task_id, stages=[*_tasks[task_id]["stages"],
                                              {"agent": f"Codex 审核 ({label})",
                                               "task_id": cx_tid,
                                               "status": cx_task["status"],
                                               "elapsed": cx_task.get("elapsed")}])
                report(f"Codex 审核 ({label})", cx_ok, review[:200])

                if re.search(r"## 通过状态:\s*(通过|有条件通过)", review):
                    passed = True
                    report("结论", True, f"审核通过，共{iteration + 1}轮")
                    break

            if not passed:
                report("结论", False, f"已达最大修正次数({MAX_ITERATIONS})")

        _update_task(task_id, status="done", result={
            "plan_preview": plan[:500],
            "code_preview": best_code[:1000],
            "review_preview": (review or "无审核")[:500],
            "passed": passed,
        })
    except Exception as e:
        _update_task(task_id, status="error", error=f"{type(e).__name__}: {e}")


# ─── MCP Server ─────────────────────────────────────────────────

mcp = MCPServer("trio-concerto")


@mcp.tool()
async def trio_concerto(task: str, workdir: str | None = None,
                        graphify: bool = False) -> str:
    """三体协奏 v2 — 三 agent 流水线（经 acp_bridge HTTP 编排）

    Hermes 调度 -> CC(思考分析) -> OpenCode(编码执行/修复) -> Codex(审核验证)

    Args:
        task: 任务描述，需要清晰完整的需求说明
        workdir: 工作目录路径（可选）
        graphify: 是否注入已有 graphify 图谱摘要（需要 workdir 且存在 graphify-out）
    """
    if len(task) > MAX_PROMPT_LEN:
        return json.dumps({"error": f"任务过长: {len(task)} > {MAX_PROMPT_LEN}"},
                          ensure_ascii=False)
    if graphify and not workdir:
        return json.dumps({"error": "graphify=True 时需要指定 workdir"},
                          ensure_ascii=False)
    task_id = _new_task(task, workdir, graphify)
    asyncio.create_task(_run_orchestration(task_id, task, workdir, graphify))
    return json.dumps({
        "task_id": task_id,
        "status": "submitted",
        "note": "异步执行中（三阶段流水线最长可超 20 分钟）。用 trio_result(task_id) 轮询，trio_list() 查看全部任务。",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def trio_result(task_id: str) -> str:
    """查询三体协奏编排任务结果（轮询用）"""
    entry = _tasks.get(task_id)
    if not entry:
        return json.dumps({"task_id": task_id, "status": "not_found"},
                          ensure_ascii=False)
    out = {k: entry[k] for k in ("task_id", "task", "status", "progress",
                                 "stages", "error", "created_at", "updated_at")}
    if entry["status"] == "done":
        out["result"] = entry["result"]
    return json.dumps(out, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
async def trio_list() -> str:
    """列出最近 20 个三体协奏编排任务"""
    items = sorted(_tasks.values(), key=lambda t: t["created_at"], reverse=True)[:20]
    return json.dumps([{
        "task_id": t["task_id"], "status": t["status"], "task": t["task"],
        "stages": len(t["progress"]), "age_s": int(time.time() - t["created_at"]),
    } for t in items], ensure_ascii=False, indent=2)


@mcp.tool()
async def trio_status() -> str:
    """查看 acp_bridge (v2 编排层) 与各 agent 状态"""
    lines = ["## 三体协奏 v2 状态\n"]
    try:
        import httpx
        r = httpx.get(f"{BRIDGE}/health", timeout=5)
        h = r.json()
        for agent, st in h.get("agents", {}).items():
            state = "degraded" if st.get("degraded") else "OK"
            lines.append(f"- {agent}: {state} (连续失败 {st.get('consecutive_failures', 0)})")
        lines.append(f"- 在飞任务: {h.get('tasks', 0)}")
    except Exception as e:
        lines.append(f"- acp_bridge (:8770) 不可达: {e}")
    running = [t for t in _tasks.values() if t["status"] in ("queued", "running")]
    if running:
        lines.append("")
        lines.append(f"### 编排中任务 ({len(running)})")
        for t in running:
            now = t["progress"][-1]["stage"] if t["progress"] else "排队中"
            lines.append(f"- [{t['task_id']}] {t['task']}  → {now}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
