"""
test_dual_model.py — 双模型路由测试

验证：
1. Session 正确携带 model（代码编写）和 aux_model（辅助任务）
2. CLI 参数解析正确设置两个模型
3. 所有辅助 LLM 调用使用 session.aux_model，而非 session.model
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock, call

from forge.core import Session, SessionMode


@pytest.fixture
def session(tmp_path):
    fd = tmp_path / ".forge"
    fd.mkdir()
    return Session(
        project_root=str(tmp_path),
        forge_dir=str(fd),
        model="qwen3-coder-plus",
        aux_model="qwen3.5-plus",
        api_key="test-key",
    )


# ── Session 字段 ─────────────────────────────────────────────────────────

def test_session_has_aux_model_field():
    """Session 必须有 aux_model 字段。"""
    s = Session(project_root="/tmp", forge_dir="/tmp/.forge")
    assert hasattr(s, "aux_model")


def test_session_default_model():
    """model 默认值为 qwen3-coder-plus。"""
    s = Session(project_root="/tmp", forge_dir="/tmp/.forge")
    assert s.model == "qwen3-coder-plus"


def test_session_default_aux_model():
    """aux_model 默认值为 qwen3.5-plus。"""
    s = Session(project_root="/tmp", forge_dir="/tmp/.forge")
    assert s.aux_model == "qwen3.5-plus"


def test_session_models_are_different():
    """两个字段默认值不同。"""
    s = Session(project_root="/tmp", forge_dir="/tmp/.forge")
    assert s.model != s.aux_model


# ── CLI 参数解析 ─────────────────────────────────────────────────────────

def test_cli_args_model_default():
    """--model 默认值为 qwen3-coder-plus。"""
    from forge.cli import parse_args
    args = parse_args.__wrapped__() if hasattr(parse_args, "__wrapped__") else None
    # 直接解析空参数列表
    import sys
    old_argv = sys.argv
    sys.argv = ["forge"]
    try:
        from forge.cli import parse_args as _parse
        args = _parse()
        assert args.model == "qwen3-coder-plus"
    finally:
        sys.argv = old_argv


def test_cli_args_aux_model_default():
    """--aux-model 默认值为 qwen3.5-plus。"""
    import sys
    old_argv = sys.argv
    sys.argv = ["forge"]
    try:
        from forge.cli import parse_args
        args = parse_args()
        assert args.aux_model == "qwen3.5-plus"
    finally:
        sys.argv = old_argv


def test_cli_args_model_override():
    """--model 参数可被覆盖。"""
    import sys
    old_argv = sys.argv
    sys.argv = ["forge", "--model", "qwen3-coder-plus-latest"]
    try:
        from forge.cli import parse_args
        args = parse_args()
        assert args.model == "qwen3-coder-plus-latest"
    finally:
        sys.argv = old_argv


def test_cli_args_aux_model_override():
    """--aux-model 参数可被覆盖。"""
    import sys
    old_argv = sys.argv
    sys.argv = ["forge", "--aux-model", "qwen-max"]
    try:
        from forge.cli import parse_args
        args = parse_args()
        assert args.aux_model == "qwen-max"
    finally:
        sys.argv = old_argv


# ── 辅助模块 LLM 调用验证 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compaction_uses_aux_model(session):
    """Level 3 compaction LLM 调用使用 aux_model。"""
    from forge.context.compaction import _compact_level_3

    session.messages = [
        {"role": "user", "content": "test " * 100},
        {"role": "assistant", "content": "reply " * 100},
    ]

    captured_model = {}

    async def fake_query_llm(**kwargs):
        captured_model["model"] = kwargs.get("model")
        r = MagicMock()
        r.text = "## Summary\nsome summary"
        return r

    with patch("forge.core.query.query_llm", new=fake_query_llm):
        await _compact_level_3(session)

    assert captured_model.get("model") == session.aux_model
    assert captured_model.get("model") != session.model


@pytest.mark.asyncio
async def test_session_memory_uses_aux_model(session):
    """Session memory 提炼使用 aux_model。"""
    from forge.context.session_memory import _extract_session_memory

    session.messages = [{"role": "user", "content": "hello"}]

    captured_model = {}

    async def fake_query_llm(**kwargs):
        captured_model["model"] = kwargs.get("model")
        r = MagicMock()
        r.text = "## Primary Request\ntest"
        return r

    with patch("forge.core.query.query_llm", new=fake_query_llm):
        await _extract_session_memory(session)

    assert captured_model.get("model") == session.aux_model


@pytest.mark.asyncio
async def test_evaluator_uses_aux_model(session):
    """Evaluator LLM 调用使用 aux_model。"""
    from forge.evaluation.evaluator import evaluate
    from forge.evaluation.criteria import DEFAULT_CRITERIA

    captured_model = {}

    async def fake_query_llm(**kwargs):
        captured_model["model"] = kwargs.get("model")
        r = MagicMock()
        r.text = '{"verdict": "pass", "score": 85, "criteria_results": [], "overall_feedback": "ok"}'
        return r

    with patch("forge.core.query.query_llm", new=fake_query_llm):
        await evaluate(session, "test input", DEFAULT_CRITERIA)

    assert captured_model.get("model") == session.aux_model


@pytest.mark.asyncio
async def test_experience_extract_uses_aux_model(session, tmp_path):
    """Experience 提炼 LLM 调用使用 aux_model。"""
    from forge.observability.experience import extract_experience
    from forge.observability.tracer import Tracer

    tracer = Tracer(session.forge_dir)
    tracer.emit("session_start", {})
    tracer.emit("evaluator_verdict", {
        "verdict": "pass", "score": 85, "criteria_results": []
    })
    tracer.emit("session_end", {"total_elapsed_s": 10.0})

    captured_model = {}

    async def fake_query_llm(**kwargs):
        captured_model["model"] = kwargs.get("model")
        r = MagicMock()
        r.text = "- 经验一\n- 经验二"
        return r

    with patch("forge.core.query.query_llm", new=fake_query_llm):
        await extract_experience(tracer.trace_file, session)

    assert captured_model.get("model") == session.aux_model


@pytest.mark.asyncio
async def test_forge_md_compiler_uses_aux_model(tmp_path):
    """FORGE.md 编译使用 aux_model 参数（不是 model）。"""
    from forge.constraints.forge_md_compiler import compile_forge_md

    captured_model = {}

    async def fake_query_llm(**kwargs):
        captured_model["model"] = kwargs.get("model")
        r = MagicMock()
        r.text = '{"permissions": {"deny": [], "ask": [], "allow": []}}'
        return r

    with patch("forge.core.query.query_llm", new=fake_query_llm):
        await compile_forge_md(
            "Some project rules",
            str(tmp_path),
            aux_model="qwen3.5-plus",
            api_key="test-key",
        )

    assert captured_model.get("model") == "qwen3.5-plus"
