"""
handoff.py — 会话结束时的交接逻辑

on_session_end 钩子：保存 session memory、progress、features，
执行 git commit，打印会话摘要。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.harness.handoff")


async def on_session_end(session: Session) -> None:
    """
    on_session_end 钩子。在会话正常退出时被调用。
    """
    from ..context.session_memory import _guarded_extract
    from ..context.progress import (
        build_progress_snapshot, save_progress, save_features,
        mark_feature_done, get_next_feature,
    )

    logger.info("Session ending, running handoff...")

    # 1. 确保 session memory 是最新的
    try:
        await _guarded_extract(session)
    except Exception as e:
        logger.warning("Session memory extraction failed during handoff: %s", e)

    # 2. 构建并保存 progress.json
    try:
        snapshot = build_progress_snapshot(session)
        save_progress(session.forge_dir, snapshot)
        logger.info("Saved progress.json")
    except Exception as e:
        logger.warning("Failed to save progress: %s", e)

    # 3. 尝试更新 features.json（粗略判断当前 feature 是否完成）
    if session.features_data:
        try:
            _try_mark_feature_done(session)
        except Exception as e:
            logger.warning("Failed to update features: %s", e)

    # 4. Git commit（如果有未提交的变更）
    await _try_git_commit(session)

    # 5. 打印会话摘要
    _print_summary(session)


def _try_mark_feature_done(session: Session) -> None:
    """
    粗略判断当前 feature 是否已完成。

    检查最后几条 assistant 消息中是否提及完成/done 等关键词。
    不需要 100% 准确——用户可以手动编辑 features.json。
    """
    from ..context.progress import get_next_feature, mark_feature_done, save_features

    current = get_next_feature(session.features_data)
    if not current:
        return

    feature_id = current.get("id", "")
    criteria = current.get("acceptance_criteria", [])
    if not criteria:
        return

    # 收集最后 5 条 assistant 文本
    recent_text = ""
    for msg in session.messages[-10:]:
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                recent_text += content + "\n"
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        recent_text += block.get("text", "") + "\n"

    recent_lower = recent_text.lower()

    # 粗略判断：如果 assistant 提到了 "完成" / "done" / "all criteria met" 等
    completion_signals = [
        "complete", "completed", "done", "finished", "all criteria",
        "完成", "已完成", "全部实现", "所有条件",
    ]

    if any(signal in recent_lower for signal in completion_signals):
        # 进一步检查：criteria 中至少一半在 assistant 文本中被提及
        mentioned = sum(1 for c in criteria if c.lower() in recent_lower)
        if mentioned >= len(criteria) / 2:
            # 获取 commit SHA
            commit_sha = _get_current_commit_sha(session.project_root)
            mark_feature_done(session.features_data, feature_id, commit_sha)
            save_features(session.forge_dir, session.features_data)
            logger.info("Feature %s auto-marked as done", feature_id)


def _get_current_commit_sha(project_root: str) -> str:
    """同步获取当前 HEAD commit SHA（用于标记 feature 完成）。"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root,
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


async def _try_git_commit(session: Session) -> None:
    """如果有未提交的变更，执行 git add -A && git commit。"""
    git_dir = Path(session.project_root) / ".git"
    if not git_dir.exists():
        return

    # 检查是否有变更
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=session.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if not stdout.decode().strip():
            logger.debug("No uncommitted changes, skipping commit")
            return
    except Exception:
        return

    # 确定 commit message
    current_feature = ""
    if session.features_data:
        from ..context.progress import get_next_feature
        nxt = get_next_feature(session.features_data)
        if nxt:
            current_feature = nxt.get("id", "unknown")

    message = f"forge: [{current_feature}] session checkpoint" if current_feature else "forge: session checkpoint"

    try:
        # git add -A
        proc = await asyncio.create_subprocess_exec(
            "git", "add", "-A",
            cwd=session.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)

        # git commit
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", message,
            cwd=session.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            print(f"  ✓ Git commit: {message}")
            logger.info("Session checkpoint committed: %s", message)
        else:
            logger.debug("Git commit returned %d", proc.returncode)
    except Exception as e:
        logger.warning("Git commit failed: %s", e)


def _print_summary(session: Session) -> None:
    """打印会话摘要。"""
    # 统计修改的文件数
    files_modified: set[str] = set()
    for msg in session.messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") == "edit_file":
                        inp = block.get("input", {})
                        if isinstance(inp, dict) and "path" in inp:
                            files_modified.add(inp["path"])

    # 当前 feature 信息
    feature_info = "N/A"
    if session.features_data:
        from ..context.progress import get_next_feature
        nxt = get_next_feature(session.features_data)
        if nxt:
            feature_info = f"{nxt.get('id')}: {nxt.get('title', '')}"
        else:
            feature_info = "All features done!"

    print(f"\n{'─' * 50}")
    print(f"  Session Summary")
    print(f"{'─' * 50}")
    print(f"  Iterations     : {session.iteration_count}")
    print(f"  Files modified : {len(files_modified)}")
    print(f"  Compactions    : {session.compaction_count}")
    print(f"  Current feature: {feature_info}")
    print(f"  Tokens (in/out): {session.total_input_tokens:,} / {session.total_output_tokens:,}")
    print(f"{'─' * 50}")
    print(f"  Next time: cd {session.project_root} && forge")
    print()
