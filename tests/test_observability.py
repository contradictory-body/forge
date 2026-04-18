"""
test_observability.py — Observability 层集成测试

覆盖：
1. Tracer 能正确写 JSONL
2. instrumentation 能捕获到事件
3. analyzer 能聚合并检测漂移
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from forge.core import Session, SessionMode, ToolCall, ToolResult
from forge.observability.tracer import Tracer, iter_events, list_session_traces
from forge.observability.analyzer import (
    summarize_session, criterion_trend, format_summary, format_trend,
)


@pytest.fixture
def session(tmp_path):
    fd = tmp_path / ".forge"
    fd.mkdir()
    return Session(
        project_root=str(tmp_path),
        forge_dir=str(fd),
        mode=SessionMode.EXECUTE,
        api_key="test-key",
    )


# ── Tracer 基础行为 ──────────────────────────────────────────────────────

def test_tracer_writes_jsonl(session):
    tracer = Tracer(session.forge_dir)
    tracer.emit("turn_start", {"turn": 1})
    tracer.emit("tool_call", {"tool": "read_file", "duration_ms": 42})

    events = list(iter_events(tracer.trace_file))
    assert len(events) == 2
    assert events[0]["type"] == "turn_start"
    assert events[0]["data"]["turn"] == 1
    assert events[1]["data"]["duration_ms"] == 42


def test_tracer_survives_disk_error(session, monkeypatch):
    """磁盘写失败时不应抛异常。"""
    tracer = Tracer(session.forge_dir)

    def bad_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.open", bad_open)
    # 不应抛异常
    tracer.emit("turn_start", {"turn": 1})
    assert tracer._disabled is True


def test_tracer_rotates_on_size(session, monkeypatch):
    """超过 ROTATE_SIZE_BYTES 后切换到轮换文件。"""
    from forge.observability import tracer as tracer_module
    monkeypatch.setattr(tracer_module, "ROTATE_SIZE_BYTES", 100)

    tracer = Tracer(session.forge_dir)
    # 写几条大事件触发轮换
    for i in range(10):
        tracer.emit("test", {"data": "x" * 50, "i": i})

    # 应该产生了轮换文件
    files = list(Path(session.forge_dir).glob("traces/*.jsonl"))
    assert len(files) >= 2


# ── instrumentation 端到端 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wrap_tool_dispatch_captures_events(session):
    """wrap_tool_dispatch 包装后工具调用会产生 tool_call 事件。"""
    from forge.observability.instrumentation import wrap_tool_dispatch
    from forge.core.tool_dispatch import ToolDispatch
    from forge.core import ToolDefinition

    # 先注册一个假工具
    async def fake_handler(sess, args):
        return ToolResult(content="ok")

    ToolDispatch.register(session, ToolDefinition(
        name="fake_tool",
        description="test",
        parameters={"type": "object", "properties": {}},
        handler=fake_handler,
        readonly=True,
    ))

    tracer = Tracer(session.forge_dir)
    session.tracer = tracer  # type: ignore
    wrap_tool_dispatch(tracer)

    tc = ToolCall(id="tc1", name="fake_tool", arguments={})
    # 权限默认 ASK → 在非交互环境会失败，改成预注册 allow
    from forge.core.permission import PermissionRule
    session.permission_rules = {
        "deny": [],
        "ask": [],
        "allow": [PermissionRule(tool="fake_tool")],
    }

    result = await ToolDispatch.execute(session, tc)
    assert not result.is_error

    events = list(iter_events(tracer.trace_file))
    tool_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["data"]["tool"] == "fake_tool"
    assert tool_events[0]["data"]["permission"] == "allow"
    assert "duration_ms" in tool_events[0]["data"]


# ── analyzer 聚合与漂移检测 ─────────────────────────────────────────────

def test_summarize_session(session):
    tracer = Tracer(session.forge_dir)
    tracer.emit("turn_start", {"turn": 1})
    tracer.emit("llm_call", {"duration_ms": 100, "input_tokens": 500, "output_tokens": 200})
    tracer.emit("tool_call", {
        "tool": "read_file",
        "duration_ms": 5,
        "is_error": False,
        "permission": "allow",
    })
    tracer.emit("tool_call", {
        "tool": "read_file",
        "duration_ms": 8,
        "is_error": False,
        "permission": "allow",
    })
    tracer.emit("tool_call", {
        "tool": "bash",
        "duration_ms": 200,
        "is_error": True,
        "permission": "ask",
    })

    stats = summarize_session(tracer.trace_file)
    assert stats["turns"] == 1
    assert stats["llm_calls"] == 1
    assert stats["llm_input_tokens"] == 500
    assert stats["tool_calls_by_name"] == {"read_file": 2, "bash": 1}
    assert stats["tool_errors"] == 1
    assert stats["permission_decisions"] == {"allow": 2, "ask": 1}

    text = format_summary(stats)
    assert "read_file" in text
    assert "Tool errors: 1" in text


def test_criterion_trend_detects_drift(tmp_path):
    """构造 N 个 trace 文件模拟评审得分下降，看漂移检测能否识别。"""
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()

    # 构造 10 个会话，前 5 次得分在 90 附近，后 5 次在 65 附近（架构合规项）
    import time
    for i in range(10):
        tracer = Tracer(str(forge_dir))
        score = 90 if i < 5 else 65
        tracer.emit("evaluator_verdict", {
            "verdict": "pass" if score >= 80 else "needs_revision",
            "score": score,
            "criteria_results": [
                {"name": "架构合规", "score": score, "passed": score >= 80},
                {"name": "功能完整性", "score": 85, "passed": True},
            ],
        })
        # 稍微延迟使 mtime 有序
        time.sleep(0.01)

    trend = criterion_trend(str(forge_dir))
    assert "架构合规" in trend
    arch = trend["架构合规"]
    assert arch["recent_mean"] == 65.0
    assert arch["prior_mean"] == 90.0
    assert arch["delta"] == -25.0
    assert arch["is_drifting"] is True

    # 功能完整性稳定，不应报警
    assert trend["功能完整性"]["is_drifting"] is False

    text = format_trend(trend)
    assert "架构合规" in text
    assert "⚠" in text


def test_criterion_trend_empty(tmp_path):
    """没 trace 数据时返回空 dict，format 可读。"""
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    trend = criterion_trend(str(forge_dir))
    assert trend == {}
    text = format_trend(trend)
    assert "no evaluation" in text
