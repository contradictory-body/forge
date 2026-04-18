"""
compaction.py — 三级 Compaction 策略

Level 1 (token_usage > 0.70): 清理旧 tool 输出
Level 2 (token_usage > 0.85): 用 session memory 替换旧历史
Level 3 (token_usage > 0.92): 派生 compaction agent 做全量摘要

注册为 session.compact_callback，由 loop.py 在每轮结束时检查调用。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.context.compaction")

# ── 阈值常量 ─────────────────────────────────────────────────────────────

L1_THRESHOLD = 0.70
L2_THRESHOLD = 0.85
L3_THRESHOLD = 0.92
L1_TARGET = 0.65       # Level 1 清理到此比例以下时停止
KEEP_TOKEN_BUDGET = 30000  # Level 2/3 保留最近消息的 token 上限
REINJECT_MAX_FILES = 3
REINJECT_MAX_CHARS = 5000


# ── 公开入口 ─────────────────────────────────────────────────────────────

async def compact(session: Session) -> None:
    """
    根据当前 token 使用率选择 compaction 级别并执行。
    """
    ratio = _get_ratio(session)
    logger.info("Compaction triggered (ratio=%.2f)", ratio)

    if ratio > L3_THRESHOLD:
        await _compact_level_3(session)
    elif ratio > L2_THRESHOLD:
        await _compact_level_2(session)
    elif ratio > L1_THRESHOLD:
        await _compact_level_1(session)
    else:
        logger.debug("Ratio %.2f below L1 threshold, skipping compaction", ratio)
        return

    session.compaction_count += 1
    logger.info(
        "Compaction #%d complete (ratio: %.2f → %.2f)",
        session.compaction_count, ratio, _get_ratio(session),
    )


# ── Level 1: 清理旧 tool 输出 ───────────────────────────────────────────

async def _compact_level_1(session: Session) -> None:
    """清理最旧的 tool_result content，替换为占位符。"""
    logger.info("Compaction Level 1: clearing old tool outputs")

    # 找到所有 tool_result 消息的索引
    tr_indices: list[int] = []
    for i, msg in enumerate(session.messages):
        content = msg.get("content")
        if (
            msg.get("role") == "user"
            and isinstance(content, list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
        ):
            tr_indices.append(i)

    cleared = 0
    for idx in tr_indices:  # 从最旧开始
        msg = session.messages[idx]
        content_list = msg["content"]
        for block in content_list:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            original = block.get("content", "")
            if isinstance(original, str) and not original.startswith("[tool output cleared"):
                char_count = len(original)
                block["content"] = f"[tool output cleared, was {char_count} chars]"
                cleared += 1

        if _get_ratio(session) < L1_TARGET:
            break

    logger.info("Level 1: cleared %d tool outputs", cleared)


# ── Level 2: 用 Session Memory 替换旧历史 ────────────────────────────────

async def _compact_level_2(session: Session) -> None:
    """用 session memory 摘要替换旧对话历史，保留最近消息。"""
    logger.info("Compaction Level 2: replacing old history with session memory")

    # 1. 执行 pre_compact 钩子
    for hook in session.hooks.get("pre_compact", []):
        try:
            await hook(session)
        except Exception as e:
            logger.warning("pre_compact hook error: %s", e)

    # 如果没有 session memory，先尝试 Level 1 再看
    if not session.session_memory_content:
        logger.warning("No session memory available, falling through to Level 1 first")
        await _compact_level_1(session)
        if _get_ratio(session) > L2_THRESHOLD:
            # 仍超标则强制 Level 3
            await _compact_level_3(session)
        return

    # 2. 确定保留边界：从后往前累计 token，不超过 KEEP_TOKEN_BUDGET
    keep_count = _calc_keep_count(session.messages, KEEP_TOKEN_BUDGET)

    # 3. 替换旧消息
    split_point = len(session.messages) - keep_count
    if split_point <= 0:
        logger.debug("Nothing to compact (all messages within budget)")
        return

    kept_messages = session.messages[split_point:]
    boundary_msg = {
        "role": "user",
        "content": (
            f"[Previous conversation compacted. Summary:\n"
            f"{session.session_memory_content}]"
        ),
    }
    session.messages = [boundary_msg] + kept_messages
    logger.info("Level 2: replaced %d old messages, kept %d", split_point, keep_count)

    # 4. 重新注入最近读取的文件
    _reinject_files(session)

    # 5. 如果仍超标 → Level 3
    if _get_ratio(session) > L2_THRESHOLD:
        logger.info("Still above L2 threshold after Level 2, escalating to Level 3")
        await _compact_level_3(session)


# ── Level 3: 派生 compaction agent 做全量摘要 ────────────────────────────

async def _compact_level_3(session: Session) -> None:
    """派生独立 LLM 调用对全部对话做结构化摘要。"""
    logger.info("Compaction Level 3: full conversation summarization via LLM")

    # 1. 序列化消息
    serialized = _serialize_messages(session.messages)

    # 2. 构建 compaction prompt
    compaction_prompt = (
        "请按以下结构输出摘要：\n\n"
        "## Primary Request\n"
        "用户最初的请求是什么\n\n"
        "## Key Technical Decisions\n"
        "agent 做出的重要技术决策（每条一句话）\n\n"
        "## Files Modified\n"
        "修改过的文件列表（路径 + 一句话说明改了什么）\n\n"
        "## Current State\n"
        "当前工作进行到哪一步\n\n"
        "## Pending Tasks\n"
        "还有什么未完成\n\n"
        "## All User Messages\n"
        "逐条列出用户的所有原始消息（不改动原文）\n\n"
        "---\n"
        f"对话历史：\n{serialized}"
    )

    # 3. 调用 LLM（独立调用）
    try:
        from ..core.query import query_llm

        response = await query_llm(
            model=session.aux_model,
            api_key=session.api_key,
            system_prompt="你是一个对话摘要助手。请将以下 coding agent 的对话历史压缩为结构化摘要。",
            messages=[{"role": "user", "content": compaction_prompt}],
            tool_definitions=[],
        )
        summary = response.text
    except Exception as e:
        logger.error("Level 3 LLM call failed: %s, falling back to message truncation", e)
        # 回退：简单截断旧消息
        summary = _fallback_summary(session.messages)

    # 4. 替换全部旧消息
    keep_count = _calc_keep_count(session.messages, KEEP_TOKEN_BUDGET)
    split_point = len(session.messages) - keep_count
    kept_messages = session.messages[split_point:] if split_point > 0 else session.messages[-5:]

    boundary_msg = {
        "role": "user",
        "content": f"[Previous conversation compacted. Summary:\n{summary}]",
    }
    session.messages = [boundary_msg] + kept_messages

    # 5. 重新注入文件
    _reinject_files(session)

    # 6. 更新 session memory
    session.session_memory_content = summary
    logger.info("Level 3: full summarization complete (%d chars)", len(summary))


# ── 辅助函数 ─────────────────────────────────────────────────────────────

def _get_ratio(session: Session) -> float:
    """获取当前 token 使用率。"""
    return session.token_usage_ratio


def _calc_keep_count(messages: list[dict], token_budget: int) -> int:
    """
    从后往前累计 token，返回在 budget 内能保留的消息条数。
    至少保留 2 条。
    """
    total = 0
    for i in range(len(messages) - 1, -1, -1):
        content = messages[i].get("content", "")
        if isinstance(content, list):
            chars = sum(len(str(b.get("content", "") or b.get("text", ""))) for b in content if isinstance(b, dict))
        else:
            chars = len(str(content))
        tokens = chars // 4 + 4  # rough estimate + per-message overhead
        if total + tokens > token_budget:
            keep = len(messages) - i - 1
            return max(keep, 2)
        total += tokens
    return len(messages)


def _reinject_files(session: Session) -> None:
    """重新注入最近读取的文件到对话历史。"""
    files_to_reinject = session.recently_read_files[:REINJECT_MAX_FILES]
    for file_path in files_to_reinject:
        try:
            p = Path(file_path)
            if not p.exists():
                continue
            content = p.read_text(encoding="utf-8")[:REINJECT_MAX_CHARS]
            rel = str(p.relative_to(session.project_root)) if session.project_root else p.name
            session.messages.append({
                "role": "user",
                "content": f"[Re-loaded after compaction] {rel}:\n{content}",
            })
            logger.debug("Re-injected file: %s", rel)
        except Exception as e:
            logger.debug("Failed to re-inject %s: %s", file_path, e)


def _serialize_messages(messages: list[dict]) -> str:
    """将消息列表序列化为文本，用于 Level 3 摘要。"""
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
                        texts.append(str(t)[:500])
            content = " | ".join(texts)
        else:
            content = str(content)[:2000]
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def _fallback_summary(messages: list[dict]) -> str:
    """当 LLM 不可用时的回退摘要：提取所有用户消息。"""
    user_msgs = [
        str(m.get("content", ""))[:200]
        for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    return "## User Messages (fallback summary)\n" + "\n".join(
        f"- {msg}" for msg in user_msgs
    )
