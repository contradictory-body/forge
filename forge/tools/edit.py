"""
edit.py — 文件编辑工具

edit_file: str_replace 语义的精确编辑。old_str 为空时创建新文件。
编辑前自动创建 git snapshot 支持回滚。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.tools.edit")


def _resolve_safe(project_root: str, rel_path: str) -> tuple[Path | None, str]:
    root = Path(project_root).resolve()
    target = (root / rel_path).resolve()
    if not str(target).startswith(str(root)):
        return None, "路径超出项目范围"
    return target, ""


async def _git_snapshot(cwd: str, metadata: dict) -> None:
    """尝试创建 git stash snapshot（不改变工作区）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "stash", "create",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        sha = stdout.decode().strip()
        if sha:
            # 保存 ref 以便 stash pop 可用
            await asyncio.create_subprocess_exec(
                "git", "stash", "store", "-m", "forge-snapshot", sha,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            metadata["last_snapshot_sha"] = sha
            logger.debug("Git snapshot created: %s", sha[:8])
    except FileNotFoundError:
        pass  # git not installed
    except Exception as e:
        logger.debug("Git snapshot failed: %s", e)


async def _handler(session: Session, arguments: dict) -> ToolResult:
    rel_path: str = arguments.get("path", "")
    old_str: str = arguments.get("old_str", "")
    new_str: str = arguments.get("new_str", "")

    if not rel_path:
        return ToolResult(content="缺少 path 参数", is_error=True)

    target, err = _resolve_safe(session.project_root, rel_path)
    if err:
        return ToolResult(content=err, is_error=True)
    assert target is not None

    # ── 创建新文件 ──
    if not old_str:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_str, encoding="utf-8")
        return ToolResult(content=f"✓ Created {rel_path} ({len(new_str)} chars)")

    # ── 编辑已有文件 ──
    if not target.exists():
        return ToolResult(
            content=f"文件不存在: {rel_path}。old_str 非空但文件不存在。",
            is_error=True,
        )

    try:
        content = target.read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(content=f"读取失败: {e}", is_error=True)

    count = content.count(old_str)
    if count == 0:
        return ToolResult(
            content="old_str 未在文件中找到。请使用 read_file 查看文件当前内容再重试。",
            is_error=True,
        )
    if count > 1:
        return ToolResult(
            content=f"old_str 在文件中出现了 {count} 次，必须唯一。请提供更多上下文以唯一定位。",
            is_error=True,
        )

    # 编辑前 git snapshot
    await _git_snapshot(session.project_root, session.metadata)

    # 执行替换
    new_content = content.replace(old_str, new_str, 1)
    target.write_text(new_content, encoding="utf-8")

    return ToolResult(
        content=f"✓ Edited {rel_path}: replaced {len(old_str)} chars with {len(new_str)} chars"
    )


TOOL_DEF = ToolDefinition(
    name="edit_file",
    description=(
        "Edit a file using str_replace semantics: provide old_str (must be unique in file) "
        "and new_str. Set old_str to empty string to create a new file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to project root.",
            },
            "old_str": {
                "type": "string",
                "description": "Exact string to replace (must appear exactly once). Empty to create new file.",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement string. Empty to delete the matched section.",
            },
        },
        "required": ["path", "old_str", "new_str"],
    },
    handler=_handler,
    readonly=False,
)
