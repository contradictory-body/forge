"""
session_memory.py — Session Memory 异步维护

每 5 次工具调用后，后台提取当前对话的结构化摘要，
写入 .forge/session_memory.md 并更新 session.session_memory_content。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.context.session_memory")

# ── 模块级状态 ───────────────────────────────────────────────────────────

_tool_call_counter: int = 0
_extraction_lock = asyncio.Lock()

# 摘要提取的 system prompt
_EXTRACT_SYSTEM = "你是一个对话摘要助手。请将以下对话提取为结构化 session memory。"

_EXTRACT_USER_TEMPLATE = """将以下对话提取为结构化 session memory，控制在 2000 tokens 以内。

输出格式：
## Primary Request
用户最初的请求

## Key Technical Decisions
agent 的重要技术决策（每条一句话）

## Files Modified
修改过的文件列表

## Current State
当前进度

## Pending Tasks
未完成事项

## All User Messages
用户的原始消息（逐条列出）

---
对话片段：
{conversation}"""


# ── 公开 API ─────────────────────────────────────────────────────────────

async def on_pre_compact(session: Session) -> None:
    """
    pre_compact 钩子：在 compaction 之前确保 session memory 是最新的。
    如果上一次提取还在进行中，等待它完成。
    """
    async with _extraction_lock:
        await _extract_session_memory(session)


async def maybe_extract(session: Session) -> None:
    """
    由 post_edit / post_bash 钩子调用。
    每 5 次工具调用触发一次后台提取。
    """
    global _tool_call_counter
    _tool_call_counter += 1
    if _tool_call_counter % 5 == 0:
        asyncio.create_task(_guarded_extract(session))


def reset_counter() -> None:
    """重置计数器（用于测试）。"""
    global _tool_call_counter
    _tool_call_counter = 0


# ── 内部实现 ─────────────────────────────────────────────────────────────

async def _guarded_extract(session: Session) -> None:
    """加锁后调用 _extract_session_memory。"""
    async with _extraction_lock:
        await _extract_session_memory(session)


async def _extract_session_memory(session: Session) -> None:
    """
    提取当前对话的结构化摘要。

    1. 序列化最近 20 条 messages
    2. 调用 LLM（独立调用）
    3. 写入 .forge/session_memory.md
    4. 更新 session.session_memory_content
    """
    if not session.messages:
        return

    # 1. 序列化最近 20 条
    recent = session.messages[-20:]
    conversation = _serialize_recent(recent)

    # 2. 调用 LLM
    try:
        from ..core.query import query_llm

        prompt = _EXTRACT_USER_TEMPLATE.format(conversation=conversation)
        response = await query_llm(
            model=session.aux_model,
            api_key=session.api_key,
            system_prompt=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tool_definitions=[],
        )
        result = response.text
    except Exception as e:
        logger.warning("Session memory extraction LLM call failed: %s", e)
        # 回退：简单提取用户消息
        result = _fallback_extract(recent)

    if not result:
        return

    # 3. 写入文件
    try:
        mem_path = Path(session.forge_dir) / "session_memory.md"
        mem_path.write_text(result, encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write session_memory.md: %s", e)

    # 4. 更新 session
    session.session_memory_content = result
    logger.info("Session memory updated (%d chars)", len(result))


def _serialize_recent(messages: list[dict]) -> str:
    """将最近消息序列化为紧凑文本。"""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text", "") or block.get("content", "")
                    if t:
                        texts.append(str(t)[:300])
            content = " | ".join(texts)
        else:
            content = str(content)[:1000]
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def _fallback_extract(messages: list[dict]) -> str:
    """LLM 不可用时的回退摘要。"""
    user_msgs = []
    files_modified = set()
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            user_msgs.append(msg["content"][:200])
        # 提取编辑过的文件
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") == "edit_file":
                        inp = block.get("input", {})
                        if isinstance(inp, dict) and "path" in inp:
                            files_modified.add(inp["path"])

    parts = ["## Primary Request", user_msgs[0] if user_msgs else "(unknown)", ""]
    if files_modified:
        parts.append("## Files Modified")
        parts.extend(f"- {f}" for f in sorted(files_modified))
        parts.append("")
    parts.append("## All User Messages")
    parts.extend(f"- {m}" for m in user_msgs)
    return "\n".join(parts)
