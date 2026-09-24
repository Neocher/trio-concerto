"""P1a 空转误判修复单测 — git diff 回退判定 (C1)。

覆盖三条路径:
1. output 空 + git 有改动 → 静默交付 (不判空转, 正常进审核)
2. output 空 + git 无改动 → 空转 (维持旧语义, 计 streak)
3. 非 git 仓库 (git 命令失败) → 空转 (异常视为无变化)

被测对象是 /home/user/trio-concerto.py 中的两个纯函数:
- `_classify_stall(output, porcelain)` — 判定逻辑 (编排层调用点)
- `_git_status_porcelain(workdir)` — git 副作用探测 (mock subprocess.run)
"""
import importlib.util
import os
from types import SimpleNamespace
from unittest import mock

_MOD_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "trio-concerto.py")
)


def _load_module():
    spec = importlib.util.spec_from_file_location("trio_concerto", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trio = _load_module()


# ── 三路径判定 (mock subprocess.run 的 git 调用) ─────────────────────

def _classify_via_git(run_result):
    """用 mock 的 subprocess.run 返回值走一遍 探测→判定 全链路, 返回 (silent, changed)。"""
    with mock.patch("subprocess.run", return_value=run_result) as m_run:
        porcelain = _trio._git_status_porcelain("/tmp")
        return _trio._classify_stall("", porcelain)


def test_path_git_has_changes_silent_delivery():
    """路径1: git 有改动 → 静默交付 (silent=True, 不判空转)"""
    run_result = SimpleNamespace(returncode=0, stdout=" M foo.py\n?? bar.py\n", stderr="")
    silent, changed = _classify_via_git(run_result)
    assert silent is True, "git 有改动应判静默交付"
    assert changed == 2


def test_path_git_no_changes_stall():
    """路径2: git 无改动 → 空转 (silent=False)"""
    run_result = SimpleNamespace(returncode=0, stdout="", stderr="")
    silent, changed = _classify_via_git(run_result)
    assert silent is False, "git 无改动应维持空转语义"
    assert changed == 0


def test_path_non_git_repo_stall():
    """路径3: 非 git 仓库 (returncode!=0) → 空转 (silent=False, 异常视为无变化)"""
    run_result = SimpleNamespace(returncode=128, stdout="",
                                 stderr="fatal: not a git repository")
    silent, changed = _classify_via_git(run_result)
    assert silent is False, "非 git 仓库应维持空转语义"
    assert changed == 0


# ── 判定逻辑纯函数 (直接覆盖 output 非空 → 原路径不变) ──────────────

def test_classify_output_nonempty_normal_path():
    """output 非空 → 正常交付, 不触发 git 判定 (silent=False)"""
    silent, changed = _trio._classify_stall("some code", "")
    assert silent is False
    assert changed == 0


def test_git_status_porcelain_timeout_returns_empty():
    """subprocess 超时/异常 → 空串 (视为无变化, 宁误判不卡死)"""
    with mock.patch("subprocess.run", side_effect=TimeoutError("boom")):
        assert _trio._git_status_porcelain("/tmp") == ""
