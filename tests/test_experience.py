"""
test_experience.py — Agent 自我经验系统测试

覆盖：
1. 提炼条件判断（有/无 evaluator_verdict，有/无 session_end）
2. .done sidecar 标记机制
3. 经验文件写入格式
4. 合并触发条件（总字符超阈值）
5. load_experience_for_prompt 读取顺序（最新在前）
6. cleanup_expired_traces 的安全删除逻辑
7. assembler Layer 6 注入
"""

from __future__ import annotations

import json
import time
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from forge.observability.experience import (
    extract_experience,
    load_experience_for_prompt,
    cleanup_expired_traces,
    startup_experience_tasks,
    EXPERIENCE_DIR_NAME,
    EXPERIENCE_MERGE_THRESHOLD,
    _find_pending_traces,
    _mark_done,
)
from forge.observability.tracer import Tracer, TRACE_DIR_NAME
from forge.core import Session, SessionMode


@pytest.fixture
def session(tmp_path):
    fd = tmp_path / ".forge"
    fd.mkdir()
    return Session(
        project_root=str(tmp_path),
        forge_dir=str(fd),
        mode=SessionMode.EXECUTE,
        api_key="test-key",
        model="test-model",
    )


def _make_trace(forge_dir: str, with_eval: bool = True, with_session_end: bool = True) -> Path:
    """创建一个测试用 trace 文件。"""
    tracer = Tracer(forge_dir)
    tracer.emit("session_start", {"mode": "execute"})
    tracer.emit("tool_call", {"tool": "read_file", "is_error": False, "duration_ms": 5})
    if with_eval:
        tracer.emit("evaluator_verdict", {
            "verdict": "needs_revision",
            "score": 62,
            "criteria_results": [
                {"name": "架构合规", "score": 40, "passed": False,
                 "feedback": "handler 直接调用了 repository"},
                {"name": "测试通过", "score": 80, "passed": True, "feedback": "OK"},
            ],
        })
    if with_session_end:
        tracer.emit("session_end", {"total_elapsed_s": 120.0})
    return tracer.trace_file


# ── _find_pending_traces ─────────────────────────────────────────────────

def test_find_pending_traces_with_eval(session):
    """有 evaluator_verdict + session_end 且无 .done → 应被找到。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)
    trace_dir = Path(session.forge_dir) / TRACE_DIR_NAME
    pending = _find_pending_traces(trace_dir)
    assert trace_file in pending


def test_find_pending_traces_no_eval(session):
    """无 evaluator_verdict → 不应被找到（不值得提炼）。"""
    _make_trace(session.forge_dir, with_eval=False, with_session_end=True)
    trace_dir = Path(session.forge_dir) / TRACE_DIR_NAME
    pending = _find_pending_traces(trace_dir)
    assert pending == []


def test_find_pending_traces_no_session_end(session):
    """无 session_end（会话未完整结束）→ 不应被找到。"""
    _make_trace(session.forge_dir, with_eval=True, with_session_end=False)
    trace_dir = Path(session.forge_dir) / TRACE_DIR_NAME
    pending = _find_pending_traces(trace_dir)
    assert pending == []


def test_find_pending_traces_already_done(session):
    """已有 .done sidecar → 不应再次处理。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)
    _mark_done(trace_file)
    trace_dir = Path(session.forge_dir) / TRACE_DIR_NAME
    pending = _find_pending_traces(trace_dir)
    assert pending == []


# ── extract_experience ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_experience_writes_file(session, tmp_path):
    """LLM 返回有效经验时，应写入 experience/ 目录并创建 .done 标记。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)

    mock_response = MagicMock()
    mock_response.text = "- 编辑 handler 前先确认不会跨层调用 repository\n- 完成后逐条核对 acceptance_criteria"

    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_response)):
        await extract_experience(trace_file, session)

    # .done 标记应存在
    assert trace_file.with_suffix(".done").exists()

    # experience 文件应存在
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    exp_files = list(exp_dir.glob("exp_*.md"))
    assert len(exp_files) == 1

    content = exp_files[0].read_text(encoding="utf-8")
    assert "merged: false" in content
    assert "evaluations:" in content
    assert "handler" in content or "acceptance" in content


@pytest.mark.asyncio
async def test_extract_experience_llm_fails_marks_done(session):
    """LLM 失败时也应创建 .done 标记（避免重复尝试）。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)

    with patch("forge.core.query.query_llm", new=AsyncMock(side_effect=RuntimeError("api error"))):
        await extract_experience(trace_file, session)

    assert trace_file.with_suffix(".done").exists()
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    assert not exp_dir.exists() or len(list(exp_dir.glob("exp_*.md"))) == 0


@pytest.mark.asyncio
async def test_extract_experience_no_value_skips_write(session):
    """LLM 返回"无可提炼"时，不写 experience 文件，但创建 .done。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)

    mock_response = MagicMock()
    mock_response.text = "（本次会话无可提炼的经验）"

    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_response)):
        await extract_experience(trace_file, session)

    assert trace_file.with_suffix(".done").exists()
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    assert not exp_dir.exists() or len(list(exp_dir.glob("exp_*.md"))) == 0


# ── load_experience_for_prompt ───────────────────────────────────────────

def test_load_experience_empty(session):
    """无经验文件时返回空字符串。"""
    result = load_experience_for_prompt(session.forge_dir)
    assert result == ""


def test_load_experience_returns_content(session):
    """有经验文件时返回其内容（去掉 front matter）。"""
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    exp_dir.mkdir()
    (exp_dir / "exp_20260401_120000.md").write_text(
        "---\nmerged: false\nevaluations: 2\n---\n\n## 经验教训\n\n- 规则A\n",
        encoding="utf-8",
    )
    result = load_experience_for_prompt(session.forge_dir)
    assert "规则A" in result
    # front matter 不应出现
    assert "merged:" not in result


def test_load_experience_newest_first(session):
    """多个文件时最新的内容应在最前面。"""
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    exp_dir.mkdir()

    f1 = exp_dir / "exp_20260401_100000.md"
    f1.write_text("---\nmerged: false\n---\n\n- 旧经验\n", encoding="utf-8")
    time.sleep(0.05)
    f2 = exp_dir / "exp_20260401_110000.md"
    f2.write_text("---\nmerged: false\n---\n\n- 新经验\n", encoding="utf-8")

    result = load_experience_for_prompt(session.forge_dir)
    # 新经验应在旧经验之前
    assert result.index("新经验") < result.index("旧经验")


# ── cleanup_expired_traces ───────────────────────────────────────────────

def test_cleanup_deletes_done_traces(session):
    """有 .done 标记且已过期的 trace 文件应被删除。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)
    _mark_done(trace_file)

    # 手动把文件 mtime 设到 8 天前
    import os
    old_time = time.time() - 8 * 86400
    os.utime(trace_file, (old_time, old_time))

    cleanup_expired_traces(session.forge_dir, retention_days=7)

    assert not trace_file.exists()
    assert not trace_file.with_suffix(".done").exists()


def test_cleanup_keeps_unextracted_traces_with_eval(session):
    """有评审事件但无 .done 标记的过期 trace 应被保留（等待提炼）。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)

    import os
    old_time = time.time() - 8 * 86400
    os.utime(trace_file, (old_time, old_time))

    cleanup_expired_traces(session.forge_dir, retention_days=7)

    # 有评审事件但没有 .done → 应保留
    assert trace_file.exists()


def test_cleanup_deletes_no_eval_traces(session):
    """无评审事件的过期 trace 可以直接删除。"""
    trace_file = _make_trace(session.forge_dir, with_eval=False, with_session_end=True)

    import os
    old_time = time.time() - 8 * 86400
    os.utime(trace_file, (old_time, old_time))

    cleanup_expired_traces(session.forge_dir, retention_days=7)

    assert not trace_file.exists()


def test_cleanup_keeps_recent_traces(session):
    """未过期的 trace 不应被删除。"""
    trace_file = _make_trace(session.forge_dir, with_eval=True, with_session_end=True)
    _mark_done(trace_file)
    # 不修改 mtime，文件是刚创建的

    cleanup_expired_traces(session.forge_dir, retention_days=7)

    assert trace_file.exists()


# ── 合并触发 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_merge_triggered_when_above_threshold(session):
    """经验文件总字符超过阈值时，应触发合并 LLM 调用。"""
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    exp_dir.mkdir()

    # 写两个大文件，确保总量超过阈值
    big_content = "---\nmerged: false\nevaluations: 3\n---\n\n" + "- 这是一条很长的经验规则条目，包含详细描述和建议\n" * 1200
    (exp_dir / "exp_20260401_100000.md").write_text(big_content, encoding="utf-8")
    time.sleep(0.05)
    (exp_dir / "exp_20260401_110000.md").write_text(big_content, encoding="utf-8")

    total = sum(f.stat().st_size for f in exp_dir.glob("exp_*.md"))
    assert total > EXPERIENCE_MERGE_THRESHOLD

    mock_response = MagicMock()
    mock_response.text = "## 高可信规律\n- 合并后的规律\n\n## 近期观察\n- 近期现象"

    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_response)) as mock_q:
        await startup_experience_tasks(session)

    # 合并 LLM 应被调用
    assert mock_q.called

    # 合并后应只有一个文件（merged: true）
    exp_files = list(exp_dir.glob("exp_*.md"))
    assert len(exp_files) == 1
    content = exp_files[0].read_text(encoding="utf-8")
    assert "merged: true" in content


# ── assembler Layer 6 ────────────────────────────────────────────────────

def test_assembler_includes_experience_in_prompt(session):
    """assembler.build 应在 System Prompt 中包含经验内容。"""
    # 写一个经验文件
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    exp_dir.mkdir()
    (exp_dir / "exp_20260401_120000.md").write_text(
        "---\nmerged: false\n---\n\n- 避免跨层调用\n",
        encoding="utf-8",
    )

    # 用 assembler.build
    from forge.context.assembler import build
    prompt = build(session)

    assert "Agent Self-Knowledge" in prompt
    assert "避免跨层调用" in prompt


def test_assembler_no_experience_no_layer(session):
    """无经验文件时，System Prompt 不应包含 Self-Knowledge 段落。"""
    from forge.context.assembler import build
    prompt = build(session)
    assert "Agent Self-Knowledge" not in prompt
