"""
test_loop.py — Agentic Loop 核心逻辑测试

使用 mock 替换 query_llm，验证循环行为。
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from forge.core import (
    Session, SessionMode, ToolResult, ToolCall, ToolDefinition,
    LLMResponse, HookResult,
)
from forge.core.loop import run_loop, _format_assistant_message, _inject_hook_content


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def session(tmp_path):
    """创建一个测试用 Session。"""
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    s = Session(
        project_root=str(tmp_path),
        forge_dir=str(forge_dir),
        api_key="test-key",
        max_iterations=10,
    )
    return s


def _make_echo_tool() -> ToolDefinition:
    """创建一个简单的 echo 工具用于测试。"""
    async def echo_handler(session, arguments):
        return ToolResult(content=f"echo: {arguments.get('text', '')}")

    return ToolDefinition(
        name="echo",
        description="Echo back the input text",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=echo_handler,
        readonly=True,
    )


def _make_write_tool() -> ToolDefinition:
    """创建一个写操作工具（readonly=False）用于 Plan Mode 测试。"""
    async def write_handler(session, arguments):
        return ToolResult(content="written")

    return ToolDefinition(
        name="do_write",
        description="A write operation",
        parameters={"type": "object", "properties": {}},
        handler=write_handler,
        readonly=False,
    )


# ── Test Cases ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("forge.core.loop.query_llm")
async def test_pure_text_reply(mock_query, session):
    """纯文本回复：无 tool_calls → 直接返回文本。"""
    mock_query.return_value = LLMResponse(
        text="Hello, I can help you!",
        tool_calls=[],
        input_tokens=100,
        output_tokens=20,
    )

    result = await run_loop(session, "hi")

    assert result == "Hello, I can help you!"
    assert len(session.messages) == 2  # user + assistant
    assert session.messages[0]["role"] == "user"
    assert session.messages[0]["content"] == "hi"
    assert session.messages[1]["role"] == "assistant"
    assert session.messages[1]["content"] == "Hello, I can help you!"
    assert session.total_input_tokens == 100
    assert session.total_output_tokens == 20
    assert session.iteration_count == 1


@pytest.mark.asyncio
@patch("forge.core.loop.query_llm")
async def test_single_tool_call(mock_query, session):
    """单次工具调用：一轮 tool_call + 第二轮纯文本。"""
    session.tool_registry["echo"] = _make_echo_tool()
    # 第一次 query_llm 也要加 allow 权限
    session.permission_rules["allow"].append(
        MagicMock(tool="echo", pattern="", path="")
    )

    # 修改 check_permission 使其对 echo 返回 ALLOW
    from forge.core.permission import PermissionRule
    session.permission_rules = {
        "deny": [],
        "ask": [],
        "allow": [PermissionRule(tool="echo")],
    }

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(
                text="Let me echo that.",
                tool_calls=[ToolCall(id="tc_1", name="echo", arguments={"text": "hello"})],
                input_tokens=50,
                output_tokens=30,
            )
        else:
            return LLMResponse(text="Done echoing.", tool_calls=[], input_tokens=80, output_tokens=10)

    mock_query.side_effect = side_effect

    result = await run_loop(session, "echo hello")

    assert result == "Done echoing."
    assert call_count == 2
    # messages: user, assistant(tool_use), tool_result, assistant(text)
    assert len(session.messages) == 4
    assert session.iteration_count == 2


@pytest.mark.asyncio
@patch("forge.core.loop.query_llm")
async def test_multi_round_tool_calls(mock_query, session):
    """多轮工具调用：两轮 tool_call + 第三轮纯文本。"""
    session.tool_registry["echo"] = _make_echo_tool()
    from forge.core.permission import PermissionRule
    session.permission_rules = {"deny": [], "ask": [], "allow": [PermissionRule(tool="echo")]}

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return LLMResponse(
                text="",
                tool_calls=[ToolCall(id=f"tc_{call_count}", name="echo", arguments={"text": f"round{call_count}"})],
                input_tokens=50, output_tokens=30,
            )
        else:
            return LLMResponse(text="All done.", tool_calls=[], input_tokens=50, output_tokens=10)

    mock_query.side_effect = side_effect

    result = await run_loop(session, "do two rounds")

    assert result == "All done."
    assert call_count == 3
    assert session.iteration_count == 3


@pytest.mark.asyncio
@patch("forge.core.loop.query_llm")
async def test_plan_mode_blocks_write(mock_query, session):
    """Plan Mode 拦截写操作。"""
    session.mode = SessionMode.PLAN
    session.tool_registry["do_write"] = _make_write_tool()

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(
                text="",
                tool_calls=[ToolCall(id="tc_w", name="do_write", arguments={})],
                input_tokens=50, output_tokens=30,
            )
        else:
            return LLMResponse(text="OK, can't write.", tool_calls=[], input_tokens=50, output_tokens=10)

    mock_query.side_effect = side_effect

    result = await run_loop(session, "write something")

    assert result == "OK, can't write."
    # The tool_result should contain the plan mode error
    tool_results = [
        m for m in session.messages
        if isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(tool_results) == 1
    tr_content = tool_results[0]["content"][0]["content"]
    assert "Plan Mode" in tr_content


@pytest.mark.asyncio
@patch("forge.core.loop.query_llm")
async def test_tool_handler_retry(mock_query, session):
    """工具执行失败 + 重试：第一次抛异常，第二次成功。"""
    call_counter = [0]

    async def flaky_handler(session, arguments):
        call_counter[0] += 1
        if call_counter[0] == 1:
            raise RuntimeError("temporary failure")
        return ToolResult(content="success after retry")

    session.tool_registry["flaky"] = ToolDefinition(
        name="flaky", description="flaky tool",
        parameters={"type": "object", "properties": {}},
        handler=flaky_handler, readonly=True,
    )
    from forge.core.permission import PermissionRule
    session.permission_rules = {"deny": [], "ask": [], "allow": [PermissionRule(tool="flaky")]}

    lm_call = 0

    async def side_effect(*args, **kwargs):
        nonlocal lm_call
        lm_call += 1
        if lm_call == 1:
            return LLMResponse(
                text="", tool_calls=[ToolCall(id="tc_f", name="flaky", arguments={})],
                input_tokens=50, output_tokens=30,
            )
        else:
            return LLMResponse(text="Worked.", tool_calls=[], input_tokens=50, output_tokens=10)

    mock_query.side_effect = side_effect

    result = await run_loop(session, "try flaky")

    assert result == "Worked."
    assert call_counter[0] == 2  # handler called twice (fail + retry)


@pytest.mark.asyncio
@patch("forge.core.loop.query_llm")
async def test_max_iterations_limit(mock_query, session):
    """max_iterations 上限：永远返回 tool_calls → 达到上限后退出。"""
    session.max_iterations = 3
    session.tool_registry["echo"] = _make_echo_tool()
    from forge.core.permission import PermissionRule
    session.permission_rules = {"deny": [], "ask": [], "allow": [PermissionRule(tool="echo")]}

    # 永远返回 tool_call，不会自然结束
    mock_query.return_value = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="tc_inf", name="echo", arguments={"text": "loop"})],
        input_tokens=50, output_tokens=30,
    )

    result = await run_loop(session, "infinite loop")

    assert "最大迭代次数" in result
    assert session.iteration_count == 3


# ── Helper function tests ────────────────────────────────────────────────

def test_format_assistant_message():
    """验证 assistant 消息格式化。"""
    response = LLMResponse(
        text="Some text",
        tool_calls=[
            ToolCall(id="tc_1", name="read_file", arguments={"path": "foo.py"}),
        ],
    )
    msg = _format_assistant_message(response)
    assert msg["role"] == "assistant"
    assert len(msg["content"]) == 2
    assert msg["content"][0]["type"] == "text"
    assert msg["content"][1]["type"] == "tool_use"
    assert msg["content"][1]["id"] == "tc_1"


def test_inject_hook_content(session):
    """验证钩子内容注入。"""
    initial_len = len(session.messages)
    _inject_hook_content(session, "tc_1", "lint error: missing import")

    assert len(session.messages) == initial_len + 1
    last = session.messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    block = last["content"][0]
    assert block["type"] == "tool_result"
    assert "hook" in block["tool_use_id"]
    assert block["content"] == "lint error: missing import"
    assert block["is_error"] is True


# ── Hook tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("forge.core.loop.query_llm")
async def test_pre_tool_hook_abort(mock_query, session):
    """pre_tool 钩子中止工具执行。"""
    session.tool_registry["echo"] = _make_echo_tool()
    from forge.core.permission import PermissionRule
    session.permission_rules = {"deny": [], "ask": [], "allow": [PermissionRule(tool="echo")]}

    async def blocking_hook(sess, tool_call):
        return HookResult(abort=True, abort_reason="Blocked by hook")

    session.hooks["pre_tool"].append(blocking_hook)

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(
                text="",
                tool_calls=[ToolCall(id="tc_b", name="echo", arguments={"text": "hi"})],
                input_tokens=50, output_tokens=30,
            )
        else:
            return LLMResponse(text="Blocked.", tool_calls=[], input_tokens=50, output_tokens=10)

    mock_query.side_effect = side_effect

    result = await run_loop(session, "try echo")
    assert result == "Blocked."

    # Verify the tool result contains the abort reason
    tool_results = [
        m for m in session.messages
        if isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" and b.get("is_error") for b in m["content"])
    ]
    assert len(tool_results) >= 1
    assert "Blocked by hook" in tool_results[0]["content"][0]["content"]
