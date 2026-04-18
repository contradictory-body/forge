"""
loop.py — Agentic Loop 主循环

设计原则：
1. loop 本身只做"循环"，不包含任何领域逻辑
2. 所有扩展通过 Session.hooks 注入，loop 在固定节点调用钩子
3. loop 不直接 import context/ 或 constraints/ 或 evaluation/ 的任何模块
"""

from __future__ import annotations

import json
import logging
from datetime import date
from uuid import uuid4
from typing import TYPE_CHECKING

from .query import query_llm
from .tool_dispatch import ToolDispatch
from ..util.terminal_ui import print_assistant, print_tool_call, print_tool_result

if TYPE_CHECKING:
    from . import Session, LLMResponse, ToolResult, HookResult

logger = logging.getLogger("forge.core.loop")


# ── 可替换的 system prompt 构建函数 ─────────────────────────────────────
# Dev B 通过 `loop.build_system_prompt = assembler.build` 替换此默认实现

def _build_system_prompt(session: Session) -> str:
    """默认的 system prompt 构建（简单拼接）。Dev B 会替换此函数。"""
    parts = [
        "You are Forge, a terminal-based coding agent.",
        f"Current mode: {session.mode.value}",
        f"Project root: {session.project_root}",
        f"Current date: {date.today().isoformat()}",
        "",
        "Core behavior rules:",
        "- Read a file before editing it (use read_file, not bash cat).",
        "- Use edit_file with str_replace semantics for precise edits.",
        "- After editing, run tests to verify your changes.",
        "- Prefer specialized tools (read_file, search_code) over bash equivalents.",
    ]

    if session.forge_md_content:
        parts.append("\n=== Project Rules (FORGE.md) ===")
        parts.append(session.forge_md_content[:6000])
        parts.append("=== End Project Rules ===")

    if session.auto_memory_content:
        parts.append("\n=== Auto Memory ===")
        parts.append(session.auto_memory_content[:4000])
        parts.append("=== End Auto Memory ===")

    if session.session_memory_content:
        parts.append("\n=== Session Memory ===")
        parts.append(session.session_memory_content[:8000])
        parts.append("=== End Session Memory ===")

    if session.progress_data:
        parts.append("\n=== Progress ===")
        parts.append(json.dumps(session.progress_data, indent=2, ensure_ascii=False)[:3000])
        parts.append("=== End Progress ===")

    return "\n".join(parts)


build_system_prompt = _build_system_prompt


# ── Agentic Loop ────────────────────────────────────────────────────────

async def run_loop(session: Session, user_input: str) -> str:
    """
    执行一轮完整的 agentic loop。

    用户输入 → (LLM → 工具执行)* → 最终文本回复
    """
    from . import ToolResult, HookResult

    # 1. 追加用户消息
    session.messages.append({"role": "user", "content": user_input})

    # 2. 循环
    for iteration in range(session.max_iterations):
        # 2a. pre_query 钩子（供 Dev B 刷新 git status 等）
        for hook in session.hooks.get("pre_query", []):
            try:
                await hook(session)
            except Exception as e:
                logger.warning("pre_query hook error: %s", e)

        # 2a. 组装 system prompt
        system_prompt = build_system_prompt(session)

        # 2b. 调用 LLM
        tool_defs = list(session.tool_registry.values())
        response = await query_llm(
            model=session.model,
            api_key=session.api_key,
            system_prompt=system_prompt,
            messages=session.messages,
            tool_definitions=tool_defs,
        )

        # 2c. 更新 token 计数
        session.total_input_tokens += response.input_tokens
        session.total_output_tokens += response.output_tokens
        session.iteration_count += 1

        # 2d. 纯文本回复 → 结束循环
        if not response.tool_calls:
            session.messages.append({"role": "assistant", "content": response.text})
            print_assistant(response.text)
            return response.text

        # 2e. 有 tool_calls → 执行工具
        assistant_msg = _format_assistant_message(response)
        session.messages.append(assistant_msg)

        for tc in response.tool_calls:
            print_tool_call(tc.name, tc.arguments)

            # ii. pre_tool 钩子
            aborted = False
            result: ToolResult | None = None
            for hook in session.hooks.get("pre_tool", []):
                try:
                    hook_result: HookResult = await hook(session, tc)
                    if hook_result.abort:
                        result = ToolResult(content=hook_result.abort_reason, is_error=True)
                        aborted = True
                        break
                except Exception as e:
                    logger.warning("pre_tool hook error: %s", e)

            # iii. 执行工具
            if not aborted:
                result = await ToolDispatch.execute(session, tc)

            assert result is not None

            # iv. post_edit 钩子
            if tc.name == "edit_file":
                for hook in session.hooks.get("post_edit", []):
                    try:
                        hook_result = await hook(session, tc, result)
                        if hook_result.inject_content:
                            _inject_hook_content(session, tc.id, hook_result.inject_content)
                    except Exception as e:
                        logger.warning("post_edit hook error: %s", e)

            # v. post_bash 钩子
            if tc.name == "bash":
                for hook in session.hooks.get("post_bash", []):
                    try:
                        hook_result = await hook(session, tc, result)
                        if hook_result.inject_content:
                            _inject_hook_content(session, tc.id, hook_result.inject_content)
                    except Exception as e:
                        logger.warning("post_bash hook error: %s", e)

            # vi. 追加 tool_result
            session.messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result.content,
                    "is_error": result.is_error,
                }],
            })

            # vii. 打印
            print_tool_result(tc.name, result)

        # 2f. 检查是否需要 compaction
        if session.token_usage_ratio > 0.70 and session.compact_callback is not None:
            try:
                await session.compact_callback(session)
            except Exception as e:
                logger.warning("Compaction callback error: %s", e)

    # 3. 达到上限
    msg = "（已达最大迭代次数，请开始新会话）"
    session.messages.append({"role": "assistant", "content": msg})
    print_assistant(msg)
    return msg


# ── 辅助函数 ────────────────────────────────────────────────────────────

def _format_assistant_message(response: LLMResponse) -> dict:
    """将 LLM 响应格式化为 Anthropic messages 格式的 assistant 消息。"""
    content: list[dict] = []
    if response.text:
        content.append({"type": "text", "text": response.text})
    for tc in response.tool_calls:
        content.append({
            "type": "tool_use",
            "id": tc.id,
            "name": tc.name,
            "input": tc.arguments,
        })
    return {"role": "assistant", "content": content}


def _inject_hook_content(session: Session, related_tool_id: str, content: str) -> None:
    """
    将钩子产生的内容注入到对话历史中。

    对 LLM 来说看起来像一个额外的 tool_result，
    它会在下一轮推理时看到并据此行动（如修复 lint 错误）。
    """
    hook_id = f"{related_tool_id}_hook_{uuid4().hex[:8]}"
    session.messages.append({
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": hook_id,
            "content": content,
            "is_error": True,  # 标记为 error 使 LLM 更注意
        }],
    })
    logger.debug("Injected hook content (id=%s, len=%d)", hook_id, len(content))
