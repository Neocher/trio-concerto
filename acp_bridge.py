"""
Trio Concerto Bridge v2 — 流式 + 双超时 + 并行
==============================================
改进（按 design_streaming_bridge.md 任务书）：
  - 流式执行：块读(4KB) + 有界队列 + asyncio.wait 三路汇合（替换 communicate()）
  - 双超时：总超时(默认900s) / 空闲超时(默认120s)，环境变量可覆盖
  - 超时判定只在汇聚层；kill 带 reason: idle_timeout | total_timeout | error
  - 结果判定基于 returncode + 是否被杀
  - codex 与 opencode 共享同一 semaphore（底层同一 opencode 二进制）
  - /tasks/:id 实时 progress 环形缓冲(50×200)
  - cleanup 跳过 running/streaming 任务；完成后 output 截断 100KB；in-flight 上限 20

协议：
  Hermes ──ACP──→ OpenCode (opencode run --attach :8769)
  Hermes ──MCP──→ CC (claude --print, prompt 走 stdin)
  Hermes ──ACP──→ Codex (opencode 审核者角色)

环境变量（优先级: 环境变量 > 默认）：
  SHM_AGENT_TOTAL_TIMEOUT / SHM_AGENT_IDLE_TIMEOUT
  SHM_AGENT_CC_CONCURRENCY / SHM_AGENT_OC_CONCURRENCY / SHM_AGENT_CODEX_CONCURRENCY
"""

import asyncio
import json
import logging
import os
import secrets
import signal
import time
import uuid
from typing import Any, Awaitable, Callable, Coroutine, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trio-bridge")
app = FastAPI(title="Trio Concerto Bridge v2")

# ── Action Registry ──
_action_handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}


def register_action(
    name: str,
    handler: Callable[..., Coroutine[Any, Any, Any]],
) -> None:
    """注册一个命名动作处理器供 ACP 协议调用。

    Args:
        name: 动作名称（如 ``"shm:write"``）。
        handler: 异步回调，接受 ``params: dict`` 参数，返回任意可 JSON 序列化的值。

    Raises:
        ValueError: 动作名已注册。
    """
    if name in _action_handlers:
        raise ValueError(f"Action {name!r} is already registered")
    _action_handlers[name] = handler
    logger.info("Registered action: %s (%s)", name, handler.__name__)

# ── SecretStr 包装 ──

class _SecretStr:
    """Minimal secret string that masks value in repr/str/traceback."""
    def __init__(self, value: str) -> None:
        self._value = value
    def get_secret_value(self) -> str:
        return self._value
    def __bool__(self) -> bool:
        return bool(self._value)
    def __repr__(self) -> str:
        return "'*****'" if self._value else "''"
    def __str__(self) -> str:
        return "*****" if self._value else ""

# ── 认证 token（可选） ──
ACP_TOKEN = _SecretStr(os.environ.get("ACP_TOKEN", ""))

# 工作目录：环境变量可配（默认当前文件所在目录），开源后用户可指到自己的项目
_WORKDIR = os.environ.get("SHM_WORKDIR", os.path.dirname(os.path.abspath(__file__)))

# OpenCode 二进制路径：环境变量可配
def _resolve_opencode_bin() -> str:
    """解析 opencode 二进制：环境变量 > PATH 探测 > 默认路径"""
    env = os.environ.get("OPENCODE_BIN")
    if env:
        return env
    import shutil
    found = shutil.which("opencode")
    if found:
        return found
    return os.path.expanduser("~/.hermes/node/bin/opencode")


_OPENCODE_BIN = _resolve_opencode_bin()


@app.middleware("http")
async def acp_auth_middleware(request: Request, call_next):
    if ACP_TOKEN:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not secrets.compare_digest(token, ACP_TOKEN.get_secret_value()):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)

# ── Config ──

def _env_int(name: str, default: int) -> int:
    """读取整数环境变量，非法值回退默认。优先级: 环境变量 > 默认。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default


AGENT_CONFIG = {
    # 双超时：total_timeout 总超时 / idle_timeout 空闲超时（无输出字节）
    "claude-code": {"timeout": _env_int("SHM_AGENT_TOTAL_TIMEOUT", 900),
                    "idle_timeout": _env_int("SHM_AGENT_IDLE_TIMEOUT", 180),
                    "max_concurrent": _env_int("SHM_AGENT_CC_CONCURRENCY", 2),
                    "max_retries": 0},
    "opencode":    {"timeout": _env_int("SHM_AGENT_TOTAL_TIMEOUT", 900),
                    "idle_timeout": _env_int("SHM_AGENT_IDLE_TIMEOUT", 120),
                    "max_concurrent": _env_int("SHM_AGENT_OC_CONCURRENCY", 3),
                    "max_retries": 1},
    "codex":       {"timeout": _env_int("SHM_AGENT_TOTAL_TIMEOUT", 900),
                    "idle_timeout": _env_int("SHM_AGENT_IDLE_TIMEOUT", 120),
                    "max_concurrent": _env_int("SHM_AGENT_CODEX_CONCURRENCY", 3),
                    "max_retries": 1},
}
CLEANUP_AFTER = 1800  # 清理超过 30 分钟的任务

# ── 流式/内存防护常量（M2） ──
STREAM_CHUNK = 4096          # 块读大小（非 readline，防 \r 进度条/半行输出漏读）
STREAM_QUEUE = 64            # 有界队列容量（解耦 reader 与用户回调，防背压假空闲）
OUTPUT_TRUNCATE = 100 * 1024  # 完成时 output 截断上限（100KB）
MAX_PROGRESS_RING = 50       # /tasks/:id progress 环形缓冲容量
MAX_PROGRESS_CHUNK = 200     # progress 每块最大字符数
IN_FLIGHT_LIMIT = 20         # in-flight 任务数上限（超出返回 429）
MAX_OUTPUT_BUFFER = 5 * 1024 * 1024   # H2: reader 侧输出累计上限（5MB），超限只保留尾部
MAX_FULL_PROMPT = 50 * 1024           # M2: full_prompt（context+prompt）整体上限（50KB）
IDLE_TERM_GRACE = 10                  # M1: idle 触发后 SIGTERM 宽限期（秒），宽限后未退再 SIGKILL
HALF_OPEN_WINDOW = 60                 # H1: 熔断半开窗口（秒），degraded 60s 后放行 1 个探测任务

# ── Data ──

class TaskDispatch(BaseModel):
    target_agent: str
    prompt: str
    context: dict = {}
    source_agent: str = "hermes"


class ActionDispatch(BaseModel):
    """ACP 动作调用请求。"""
    params: dict[str, Any] = Field(default_factory=dict, description="传递给动作处理器的参数")

tasks: dict[str, dict] = {}
# CC H3: codex 与 opencode 共享同一把 semaphore（底层同一 opencode 二进制），
#       容量取两者 max_concurrent 的较大者（opencode 3 + codex 3 = 最多 3 并发，非 6）
_shared_opencode_sem = asyncio.Semaphore(
    max(AGENT_CONFIG["opencode"]["max_concurrent"],
        AGENT_CONFIG["codex"]["max_concurrent"])
)
agent_semaphores: dict[str, asyncio.Semaphore] = {
    "claude-code": asyncio.Semaphore(AGENT_CONFIG["claude-code"]["max_concurrent"]),
    "opencode": _shared_opencode_sem,
    "codex": _shared_opencode_sem,
}
agent_health: dict[str, dict] = {
    name: {"success": 0, "failure": 0, "consecutive_failures": 0,
           "degraded": False, "degraded_at": None, "half_open_probe": False}
    for name in AGENT_CONFIG
}

# ── Helpers ──

def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the entire process group (child + any grandchildren)."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM 整个进程组（M1：idle 触发先给宽限，让任务自行收尾而非直接 SIGKILL）。"""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

def _minimal_env() -> dict[str, str]:
    """返回最小环境变量（不含 API keys），防止子进程继承凭证。

    【FIX 2026-07-31】放行 opencodex 代理所需变量（模型统一走 :10100）：
    - DEEPSEEK_API_KEY：OpenCode (Sisyphus) 连接 DeepSeek 用
    - ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN：Claude Code 走 opencodex 代理
    - CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY：CC 网关模型发现
    """
    # 只保留运行必需的环境变量，排除所有 API 密钥
    safe_keys = {"PATH", "HOME", "USER", "TERM", "LANG", "LC_ALL",
                 "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"}
    env = {k: v for k, v in os.environ.items()
           if k in safe_keys and not k.endswith("_API_KEY")}
    # OpenCode 需要 DEEPSEEK_API_KEY（非打码的真实 key）
    real_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if real_key and "..." not in real_key:
        env["DEEPSEEK_API_KEY"] = real_key
    # Claude Code 走 opencodex 代理（ANTHROPIC_* 放行）
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
              "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env

# ── 任务进度跟踪 ──

class _ProgressTracker:
    """任务进度跟踪：更新 tasks[task_id] 的 progress 环形缓冲（50×200）。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    def reset(self, marker: str = "") -> None:
        """重试时清空进度缓冲并加 attempt 分隔标记（L5）。"""
        t = tasks.get(self.task_id)
        if t is None:
            return
        t["progress"] = [marker] if marker else []
        t["progress_ts"] = time.time()

    async def push(self, chunk: bytes) -> None:
        """消费者回调：追加一个输出块到环形缓冲，超出 50 条时淘汰最旧。"""
        t = tasks.get(self.task_id)
        if t is None:
            return
        text = chunk.decode("utf-8", errors="replace")
        t["progress"].append(text[:MAX_PROGRESS_CHUNK])
        if len(t["progress"]) > MAX_PROGRESS_RING:
            del t["progress"][: len(t["progress"]) - MAX_PROGRESS_RING]
        t["progress_ts"] = time.time()

# ── Executors ──

async def _exec_stream(agent: str, prompt: str, cwd: str, cfg: dict,
                       on_progress: Optional[Callable[[bytes], Awaitable[None]]] = None) -> dict:
    """流式执行子进程（替换 communicate()）。

    设计（按任务书 CC 修订版）：
      - 块读(4KB)而非 readline：处理 \\r 进度条 / 半行输出（readline 会漏）
      - 有界队列解耦：reader 只 queue.put_nowait(块)，绝不 await 用户回调
        → 防慢回调背压 → 管道满 → 假空闲（队列满只丢进度块，输出已在 reader 侧累积）
      - 消费：独立任务从队列取 → on_progress（更新任务进度）
      - 三路汇合：asyncio.wait([gather(readers), idle_watchdog, total_wait], FIRST_COMPLETED)
      - 超时判定只发生在汇聚层，reader 内部绝不判超时
      - kill 路径带 reason: idle_timeout | total_timeout | error
      - kill 后：杀进程组 → await proc.wait() 收尸 → cancel readers（防 zombie）
    """
    start = time.time()
    total_timeout = cfg["timeout"]
    idle_timeout = cfg["idle_timeout"]
    logger.info("[%s] Starting (total=%ds, idle=%ds, prompt=%d chars)",
                agent, total_timeout, idle_timeout, len(prompt))

    # ── 构造命令（exec_cc 走 stdin 读 prompt；opencode/codex 走参数）──
    if agent == "claude-code":
        cmd = ["claude", "-p", "-", "--print"]
        feed_stdin = True
    elif agent == "opencode":
        opencode_bin = _OPENCODE_BIN
        cmd = [opencode_bin, "run", prompt, "--model", "deepseek/deepseek-v4-flash"]
        feed_stdin = False
    elif agent == "codex":
        opencode_bin = _OPENCODE_BIN
        cmd = [opencode_bin, "run", prompt, "--model", "deepseek/deepseek-v4-flash"]
        feed_stdin = False
    else:
        raise ValueError(f"Unknown agent: {agent!r}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if feed_stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=_minimal_env(),
        preexec_fn=os.setsid,
    )

    q: asyncio.Queue = asyncio.Queue(maxsize=STREAM_QUEUE)  # 有界队列（仅承载进度块）
    out_parts: list[bytes] = []
    err_parts: list[bytes] = []
    last_activity = time.time()  # 空闲判据：按字节流，非按行

    # 【FIX 2026-08-02】共享 CPU 活跃检测：idle_watchdog + total_watchdog 共用
    def _proc_cpu_time() -> tuple[int, int]:
        """读取 /proc/PID/stat 的 utime+stime（0 基索引 13,14），失败返回 (0,0)。"""
        try:
            with open(f"/proc/{proc.pid}/stat", "rb") as f:
                parts = f.read().split()
            return int(parts[13]), int(parts[14])
        except (OSError, ValueError, IndexError):
            return 0, 0

    async def _feed_stdin() -> None:
        """显式喂 stdin → drain → close（exec_cc 必需：claude -p - 从 stdin 读 prompt）"""
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except Exception as e:
            # 含 BrokenPipeError / ConnectionResetError / ValueError(流已关闭)：
            # 喂 stdin 失败时子进程要么自己退出(rc 判定)要么空等(idle 判定)，
            # 此处只记录并收尾，绝不让异常逃逸成未取回的 task 异常
            logger.warning("[%s] stdin feed failed: %s", agent, e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def _read_stream(stream: Any, target: list[bytes]) -> None:
        """块读(4KB)。M4: 以 proc.returncode 作为提前退出条件——统一用 1s 限时读，
        进程退出后即使孙进程持有管道写端（EOF 永不出现）也能周期性重新检查
        returncode 并退出，防汇聚层空等到 idle_timeout 误杀已完成的任务。
        H2: 内存防护前移——累计超 MAX_OUTPUT_BUFFER(5MB) 后丢最旧块只保留尾部，
        不再等任务完成才截断（长任务输出不再无限膨胀内存）。"""
        nonlocal last_activity
        buffered = 0
        while True:
            try:
                chunk = await asyncio.wait_for(stream.read(STREAM_CHUNK), timeout=1.0)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break  # 进程已退出，管道残余由孙进程持有 → 不再等待 EOF
                continue   # 进程仍在运行但无输出：继续轮询（空闲判定在汇聚层）
            if not chunk:
                break
            target.append(chunk)
            buffered += len(chunk)
            while buffered > MAX_OUTPUT_BUFFER:
                dropped = target.pop(0)  # H2: 只保留尾部，丢弃最旧块
                buffered -= len(dropped)
            last_activity = time.time()
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass  # 有界队列满：丢进度块不丢输出（输出已累积在 target）

    async def _consume() -> None:
        """独立消费任务：队列取块 → on_progress。绝不阻塞 reader（reader 用 put_nowait）。"""
        while True:
            chunk = await q.get()
            try:
                if on_progress is not None:
                    await on_progress(chunk)
            except Exception:
                logger.exception("[%s] on_progress callback failed", agent)
            finally:
                q.task_done()

    async def _idle_watchdog() -> None:
        """空闲超时监控：无输出字节 + 进程 CPU 无活动 超过 idle_timeout 才触发。

        【FIX 2026-08-02】用户原则：心跳在运行就不该中断。
        之前仅按输出字节判空闲——CC 长思考（读文件→内部推理→一次性输出）
        在思考阶段无 stdout，但 CPU 活跃（正常工作中），被误判 idle 杀掉。
        现在：进程 CPU 有活动（/proc/PID/stat utime+stime 变化）= 在思考 = 重置 idle；
        只有「进程存活 + CPU 完全空闲 + 无输出 > idle_timeout」才判真空闲。
        """
        nonlocal last_activity  # 引用外层 _exec_stream 的 last_activity（_read_stream 也用它）
        last_cpu = _proc_cpu_time()
        while True:
            await asyncio.sleep(5)
            if time.time() - last_activity > idle_timeout:
                cpu_now = _proc_cpu_time()
                # 进程已退出：无 CPU 变化，视为可回收（正常退出由 reader EOF 处理）
                if proc.returncode is not None:
                    return
                # CPU 有活动 = 进程在思考/计算，重置 idle 计时
                if cpu_now != last_cpu:
                    last_activity = time.time()
                    last_cpu = cpu_now
                    continue
                # 进程存活但 CPU 完全空闲且无输出超时 → 真空闲
                return

    async def _total_watchdog() -> None:
        """总超时监控：活跃感知，到点不硬杀。

        【FIX 2026-08-02】用户原则：CPU 活跃还在运行就不杀，除非空闲才杀。
        之前是纯 asyncio.sleep(total_timeout) 定时炸弹——长任务（如 A 组
        H3/H4/H5 三问题一次做）900s 一直在真实工作，只是任务重超过预算，
        被无差别 SIGKILL（rc=-9），最后阶段的工作全部丢失。
        v2 修复（2026-08-03 用户确认原则）：total_timeout 只是「空闲兜底」——
          - 进程 CPU 活跃（/proc/PID/stat 变化）= 仍在真实工作 → 重置 deadline，永不杀
          - 进程存活 + CPU 完全空闲 + 无输出 持续 total_timeout → 真失控 → 触发
        与 _idle_watchdog 同判据，只是阈值更大（900s vs 180s）：idle 先杀真空闲，
        total 兜底「idle 没抓到的长空闲」。不再有顺延次数上限——活跃任务无限期运行。
        """
        nonlocal last_activity
        deadline = time.time() + total_timeout
        last_cpu = _proc_cpu_time()
        while True:
            await asyncio.sleep(5)
            if time.time() < deadline:
                continue
            # 到点：检查活跃度
            if proc.returncode is not None:
                return  # 进程已退出，交给 reader EOF 正常收尸
            cpu_now = _proc_cpu_time()
            active = (cpu_now != last_cpu) or (time.time() - last_activity < idle_timeout * 0.5)
            if active:
                # 进程仍在真实工作 → 重置 deadline（活跃任务无限顺延，不杀）
                deadline = time.time() + total_timeout
                logger.warning(
                    "[%s] TOTAL deadline reached but process active — resetting deadline +%ds",
                    agent, total_timeout,
                )
                last_cpu = cpu_now
                continue
            # 进程存活但 CPU 完全空闲 + 无输出持续 total_timeout → 真失控
            return

    # ── 启动子任务 ──
    feed_task = asyncio.ensure_future(_feed_stdin()) if feed_stdin else None
    r1 = asyncio.ensure_future(_read_stream(proc.stdout, out_parts))
    r2 = asyncio.ensure_future(_read_stream(proc.stderr, err_parts))
    reader_task = asyncio.ensure_future(asyncio.gather(r1, r2))
    consumer = asyncio.ensure_future(_consume())
    idle_task = asyncio.ensure_future(_idle_watchdog())
    total_task = asyncio.ensure_future(_total_watchdog())

    # ── 三路汇合（超时判定只在此层）──
    reason = ""
    try:
        done, _pending = await asyncio.wait(
            {reader_task, idle_task, total_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if reader_task in done:
            # 正常 EOF：进程应即将退出，直接走收尸
            pass
        elif total_task in done:
            reason = "total_timeout"
            logger.warning("[%s] TOTAL TIMEOUT after %ds — killing process group",
                           agent, total_timeout)
            _kill_process_group(proc)  # 杀进程组（含孙进程）
        elif idle_task in done:
            reason = "idle_timeout"
            logger.warning("[%s] IDLE TIMEOUT after %ds of no output — SIGTERM (grace %ds)",
                           agent, idle_timeout, IDLE_TERM_GRACE)
            _terminate_process_group(proc)  # M1: 先 SIGTERM 给收尾机会
            try:
                await asyncio.wait_for(proc.wait(), timeout=IDLE_TERM_GRACE)
            except asyncio.TimeoutError:
                _kill_process_group(proc)  # M1: 宽限未退 → SIGKILL
    except asyncio.CancelledError:
        # S1: 取消路径同样杀进程组（防孤儿进程/僵尸），再抛
        reason = "cancelled"
        logger.warning("[%s] CANCELLED — killing process group", agent)
        _kill_process_group(proc)
        raise
    finally:
        # S1: 收尸移入 finally（幂等）——无论正常/被杀/取消都 await proc.wait() 防 zombie
        try:
            # S2: 收尸上限用剩余总超时预算 min(30, remaining)，不用固定 5s 误杀正常收尾任务
            remaining = max(1.0, total_timeout - (time.time() - start))
            rc = await asyncio.wait_for(proc.wait(), timeout=min(30.0, remaining))
        except asyncio.TimeoutError:
            # 管道已 EOF 但进程未退出（孙进程持管道等）：强杀兜底
            _kill_process_group(proc)
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                rc = -9  # 极端情况：无法收尸，以 -9 记录
            if not reason:
                reason = "error"
        # cancel readers（防 zombie/悬挂）+ consumer + watchdog（L1）
        pending_tasks = [t for t in (feed_task, r1, r2, reader_task, consumer,
                                     idle_task, total_task)
                         if t is not None and not t.done()]
        for t in pending_tasks:
            t.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    # ── 结果判定（替代 "completed" if out else "failed"）──
    out = b"".join(out_parts).decode("utf-8", errors="replace").strip()
    err = b"".join(err_parts).decode("utf-8", errors="replace").strip()
    # M2: 完成时 output 截断 100KB
    if len(out) > OUTPUT_TRUNCATE:
        logger.info("[%s] output truncated %d → %d chars", agent, len(out), OUTPUT_TRUNCATE)
        out = out[:OUTPUT_TRUNCATE]
    if len(err) > OUTPUT_TRUNCATE:
        err = err[:OUTPUT_TRUNCATE]

    if reason:
        status = "failed"          # 超时/异常被杀
    elif rc == 0:
        status = "completed"       # 正常结束且未被杀
    else:
        status = "failed"          # 崩溃（rc != 0）
        reason = "error"

    elapsed = round(time.time() - start, 1)
    logger.info("[%s] Done in %ds rc=%s reason=%r (%d chars output)",
                agent, elapsed, rc, reason, len(out))
    return {"status": status, "output": out,
            "error": err if status == "failed" else "",
            "reason": reason, "returncode": rc, "elapsed": elapsed}


async def exec_cc(prompt: str, cwd: str, cfg: dict,
                  on_progress: Optional[Callable[[bytes], Awaitable[None]]] = None) -> dict:
    return await _exec_stream("claude-code", prompt, cwd, cfg, on_progress)


async def exec_opencode(prompt: str, cwd: str, cfg: dict,
                        on_progress: Optional[Callable[[bytes], Awaitable[None]]] = None) -> dict:
    return await _exec_stream("opencode", prompt, cwd, cfg, on_progress)


async def exec_codex(prompt: str, cwd: str, cfg: dict,
                     on_progress: Optional[Callable[[bytes], Awaitable[None]]] = None) -> dict:
    codex_prompt = f"你是一个代码审核专家(Codex Reviewer)。请审核以下任务：\n\n{prompt}\n\n仅输出审核结论。"
    return await _exec_stream("codex", codex_prompt, cwd, cfg, on_progress)


EXECUTORS = {
    "claude-code": exec_cc,
    "opencode": exec_opencode,
    "codex": exec_codex,
}

async def execute_with_retry(agent: str, prompt: str, cwd: str,
                             tracker: Optional["_ProgressTracker"] = None) -> dict:
    """带重试和并发限制的执行（超时已由 _exec_stream 内部判定，不再抛 TimeoutError）。

    M3: 任务级总预算 = cfg.timeout——重试不再各自获得完整 total_timeout，
        每次 attempt 的 timeout = min(单次超时, 剩余预算)，含退避等待在内总时长 ≤ cfg.timeout。
    H1: 熔断半开——探测任务失败时重新武装 degraded_at，成功时闭合熔断器。
    """
    cfg = AGENT_CONFIG[agent]
    sem = agent_semaphores[agent]
    deadline = time.time() + cfg["timeout"]  # M3: 任务级总预算截止时间
    result = {"status": "failed", "output": "", "error": "unknown",
              "reason": "error", "returncode": None}
    async with sem:
        for attempt in range(1 + cfg["max_retries"]):
            # L5: 重试时进度缓冲清空 + 加 attempt 分隔标记
            if tracker is not None:
                tracker.reset(marker=f"--- attempt {attempt + 1} ---")
            # M3: 剩余预算不足直接失败，不发起新 attempt
            remaining = deadline - time.time()
            if remaining <= 0:
                result = {"status": "failed", "output": "", "error": "task total timeout",
                          "reason": "total_timeout", "returncode": None}
                break
            attempt_cfg = dict(cfg, timeout=min(cfg["timeout"], remaining))
            try:
                kwargs = {
                    "prompt": prompt, "cwd": cwd, "cfg": attempt_cfg,
                    "on_progress": tracker.push if tracker is not None else None,
                }
                result = await EXECUTORS[agent](**kwargs)
                if result["status"] == "completed":
                    agent_health[agent]["success"] += 1
                    agent_health[agent]["consecutive_failures"] = 0
                    agent_health[agent]["degraded"] = False
                    agent_health[agent]["degraded_at"] = None
                    agent_health[agent]["half_open_probe"] = False
                    return result
                # 失败但还有重试机会
                if attempt < cfg["max_retries"]:
                    wait = 5 * (attempt + 1)
                    # M3: 预算不足以容纳退避等待 → 不再重试
                    if time.time() + wait >= deadline:
                        logger.warning("[%s] attempt %d failed (%s), no budget for retry",
                                       agent, attempt + 1, result.get("reason", "error"))
                        break
                    logger.warning("[%s] attempt %d failed (%s), retry in %ds",
                                   agent, attempt + 1, result.get("reason", "error"), wait)
                    await asyncio.sleep(wait)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt < cfg["max_retries"]:
                    wait = 5 * (attempt + 1)
                    if time.time() + wait >= deadline:
                        logger.warning("[%s] attempt %d error: %s, no budget for retry",
                                       agent, attempt + 1, e)
                        break
                    logger.warning("[%s] attempt %d error: %s, retry in %ds",
                                   agent, attempt + 1, e, wait)
                    await asyncio.sleep(wait)
                    continue
                result = {"status": "failed", "output": "", "error": str(e),
                          "reason": "error", "returncode": None}
        # 所有重试失败
        agent_health[agent]["failure"] += 1
        agent_health[agent]["consecutive_failures"] += 1
        if agent_health[agent]["consecutive_failures"] >= 3:
            agent_health[agent]["degraded"] = True
            # H1: 记录熔断时间，半开窗口从此刻起算；探测失败时重新武装
            agent_health[agent]["degraded_at"] = time.time()
            agent_health[agent]["half_open_probe"] = False
            logger.error("[%s] circuit breaker TRIPPED — degraded", agent)
        return result

# ── 后台清理 ──

async def cleanup_loop():
    while True:
        await asyncio.sleep(300)  # 每 5 分钟
        now = time.time()
        # M1: 跳过 running/streaming 任务，防删运行中任务 → 后续 tasks[...] KeyError
        stale = [tid for tid, t in tasks.items()
                 if now - t.get("created_at", 0) > CLEANUP_AFTER
                 and t.get("status") != "running"
                 and not t.get("streaming")]
        for tid in stale:
            tasks.pop(tid, None)
        if stale:
            logger.info("Cleaned %d stale tasks", len(stale))

# ── API ──

@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_loop())

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agents": {name: {"degraded": agent_health[name]["degraded"],
                          "consecutive_failures": agent_health[name]["consecutive_failures"]}
                   for name in EXECUTORS},
        "tasks": len(tasks),
    }

@app.post("/dispatch")
async def dispatch_task(req: TaskDispatch):
    if req.target_agent not in EXECUTORS:
        raise HTTPException(400, f"Unknown agent: {req.target_agent}")
    # 校验类拒绝必须先于熔断半开放行：探针任务必须能真正入队执行，
    # 否则 half_open_probe 置位后被 429/413 拒绝 → 标志永久卡 True，熔断器无法再闭合
    # M2: in-flight 任务数上限（超出返回 429）
    in_flight = sum(1 for t in tasks.values()
                    if t.get("status") == "running" or t.get("streaming"))
    if in_flight >= IN_FLIGHT_LIMIT:
        raise HTTPException(429, f"Too many in-flight tasks: {in_flight} (max {IN_FLIGHT_LIMIT})")
    # 防 DoS：限制 prompt 大小
    max_prompt = 10_000
    if len(req.prompt) > max_prompt:
        raise HTTPException(413, f"Prompt too large: {len(req.prompt)} chars (max {max_prompt})")
    context_str = "\n".join(f"{k}: {v}" for k, v in req.context.items())
    full_prompt = f"{context_str}\n\n{req.prompt}" if context_str else req.prompt
    # M2: full_prompt（context+prompt）整体限 50KB，防超大 context 撑爆 stdin drain/内存
    if len(full_prompt) > MAX_FULL_PROMPT:
        raise HTTPException(413,
                            f"Full prompt too large: {len(full_prompt)} chars (max {MAX_FULL_PROMPT})")
    health = agent_health[req.target_agent]
    if health["degraded"]:
        # H1: 半开恢复——degraded 60s 后放行 1 个探测任务，成功即闭合熔断器
        if (health.get("half_open_probe")
                or time.time() - (health.get("degraded_at") or time.time()) < HALF_OPEN_WINDOW):
            raise HTTPException(503, f"Agent {req.target_agent} is degraded (circuit breaker open)")
        health["half_open_probe"] = True
        logger.info("[%s] circuit half-open — allowing 1 probe task", req.target_agent)
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    now = time.time()
    tasks[task_id] = {"id": task_id, "agent": req.target_agent,
                      "status": "running", "streaming": True,
                      "progress": [], "progress_ts": now, "reason": "",
                      "created_at": now}

    async def run():
        tracker = _ProgressTracker(task_id)
        try:
            result = await execute_with_retry(req.target_agent, full_prompt,
                                              _WORKDIR, tracker)
            result["elapsed"] = round(time.time() - now, 1)
            t = tasks.get(task_id)  # M1: tasks.get 判空，防 cleanup 并发删除
            if t is not None:
                t.update(result)
                t["status"] = result["status"]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("[%s] task %s crashed", req.target_agent, task_id)
            t = tasks.get(task_id)
            if t is not None:
                t.update({"status": "failed", "reason": "error", "error": str(e),
                          "output": "", "returncode": None,
                          "elapsed": round(time.time() - now, 1)})
        finally:
            t = tasks.get(task_id)
            if t is not None:
                t["streaming"] = False
                # S1 FIX (Codex #1): 取消/异常路径下 status 仍为 running →
                # 永久占 in-flight 槽位（20 次后永久 429）+ cleanup 跳过永不清理
                # + half_open_probe 卡 True（探测任务被取消熔断器永不闭合）。
                # 兜底：置 failed 落盘释放槽位；探测任务被取消时复位半开标志。
                if t.get("status") == "running":
                    t.update({"status": "failed", "reason": "cancelled",
                              "error": "task cancelled", "output": "",
                              "returncode": None,
                              "elapsed": round(time.time() - now, 1)})
                    agent_health[req.target_agent]["half_open_probe"] = False
                    logger.warning("[%s] task %s aborted — marked failed, slot released",
                                   req.target_agent, task_id)

    asyncio.create_task(run())
    return {"task_id": task_id, "agent": req.target_agent, "status": "dispatched"}

@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return t

@app.get("/agents")
async def list_agents():
    return {
        name: {
            **agent_health[name],
            "config": AGENT_CONFIG[name],
        }
        for name in EXECUTORS
    }

@app.post("/reset/{agent}")
async def reset_agent(agent: str):
    if agent not in EXECUTORS:
        raise HTTPException(400, f"Unknown agent: {agent}")
    agent_health[agent] = {"success": 0, "failure": 0,
                           "consecutive_failures": 0, "degraded": False,
                           "degraded_at": None, "half_open_probe": False}
    logger.info("[%s] health reset", agent)
    return {"status": "reset", "agent": agent}

# ── Action Dispatch ──

@app.get("/actions")
async def list_actions():
    """列出所有已注册的 ACP 动作。"""
    return {"actions": sorted(_action_handlers.keys())}


@app.post("/action/{action_name:path}")
async def dispatch_action(action_name: str, req: ActionDispatch):
    """调用已注册的 ACP 动作处理器。

    Args:
        action_name: 动作名称（如 ``shm:write``）。
        req: 包含 ``params`` 字典的请求体。

    Returns:
        动作处理器的返回值。

    Raises:
        HTTPException 404: 动作未注册。
        HTTPException 500: 动作执行异常。
    """
    handler = _action_handlers.get(action_name)
    if handler is None:
        raise HTTPException(404, f"Unknown action: {action_name!r}")

    logger.info("Dispatching action: %s", action_name)
    try:
        if asyncio.iscoroutinefunction(handler):
            result = await asyncio.wait_for(handler(req.params), timeout=30)
        else:
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, handler, req.params),
                timeout=30,
            )
        return {"action": action_name, "status": "ok", "result": result}
    except asyncio.TimeoutError:
        logger.warning("Action %s timed out after 30s", action_name)
        raise HTTPException(504, f"Action {action_name!r} timed out")
    except Exception as exc:
        logger.exception("Action %s failed", action_name)
        raise HTTPException(500, f"Action {action_name!r} failed: {exc}")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8770, log_level="info")
