"""
git.py — Git 操作工具

导出 4 个 ToolDefinition:
  git_status (readonly), git_diff (readonly), git_log (readonly), git_commit (write)

另导出 git_restore_snapshot() 辅助函数供 /undo 命令使用。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.tools.git")


def _is_git_repo(project_root: str) -> bool:
    return (Path(project_root) / ".git").exists()


async def _run_git(cwd: str, *args: str, timeout: int = 15) -> tuple[int, str]:
    """执行 git 命令，返回 (returncode, combined_output)。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "git 命令超时"
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    combined = out if not err else f"{out}\n{err}".strip()
    return proc.returncode or 0, combined


# ── git_status ───────────────────────────────────────────────────────────

async def _status_handler(session: Session, arguments: dict) -> ToolResult:
    if not _is_git_repo(session.project_root):
        return ToolResult(content="当前目录不是 git 仓库", is_error=True)
    rc, out = await _run_git(session.project_root, "status", "--short")
    if rc != 0:
        return ToolResult(content=out, is_error=True)
    return ToolResult(content=out if out.strip() else "(working tree clean)")


_status_def = ToolDefinition(
    name="git_status",
    description="Show git working tree status (short format).",
    parameters={"type": "object", "properties": {}},
    handler=_status_handler,
    readonly=True,
)


# ── git_diff ─────────────────────────────────────────────────────────────

async def _diff_handler(session: Session, arguments: dict) -> ToolResult:
    if not _is_git_repo(session.project_root):
        return ToolResult(content="当前目录不是 git 仓库", is_error=True)
    args = ["diff"]
    if arguments.get("staged"):
        args.append("--staged")
    rc, out = await _run_git(session.project_root, *args)
    if rc != 0:
        return ToolResult(content=out, is_error=True)
    return ToolResult(content=out if out.strip() else "(no diff)")


_diff_def = ToolDefinition(
    name="git_diff",
    description="Show git diff of uncommitted changes. Pass staged=true for staged changes.",
    parameters={
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "If true, show staged changes only.",
            },
        },
    },
    handler=_diff_handler,
    readonly=True,
)


# ── git_log ──────────────────────────────────────────────────────────────

async def _log_handler(session: Session, arguments: dict) -> ToolResult:
    if not _is_git_repo(session.project_root):
        return ToolResult(content="当前目录不是 git 仓库", is_error=True)
    count = min(arguments.get("count", 20), 50)
    rc, out = await _run_git(session.project_root, "log", "--oneline", f"-{count}")
    if rc != 0:
        return ToolResult(content=out, is_error=True)
    return ToolResult(content=out if out.strip() else "(no commits)")


_log_def = ToolDefinition(
    name="git_log",
    description="Show recent git commit log (oneline format).",
    parameters={
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "Number of commits to show (default 20, max 50).",
            },
        },
    },
    handler=_log_handler,
    readonly=True,
)


# ── git_commit ───────────────────────────────────────────────────────────

async def _commit_handler(session: Session, arguments: dict) -> ToolResult:
    if not _is_git_repo(session.project_root):
        return ToolResult(content="当前目录不是 git 仓库", is_error=True)
    message: str = arguments.get("message", "forge: checkpoint")
    if not message:
        message = "forge: checkpoint"

    # git add -A
    rc, out = await _run_git(session.project_root, "add", "-A")
    if rc != 0:
        return ToolResult(content=f"git add failed: {out}", is_error=True)

    # git commit
    rc, out = await _run_git(session.project_root, "commit", "-m", message)
    if rc != 0:
        # rc=1 with "nothing to commit" is not a real error
        if "nothing to commit" in out:
            return ToolResult(content="Nothing to commit (working tree clean)")
        return ToolResult(content=f"git commit failed: {out}", is_error=True)

    return ToolResult(content=f"✓ Committed: {message}\n{out}")


_commit_def = ToolDefinition(
    name="git_commit",
    description="Stage all changes and commit with a message.",
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Commit message.",
            },
        },
        "required": ["message"],
    },
    handler=_commit_handler,
    readonly=False,
)


# ── 导出 ─────────────────────────────────────────────────────────────────

TOOL_DEFS: list[ToolDefinition] = [_status_def, _diff_def, _log_def, _commit_def]


# ── /undo 辅助函数（非工具）──────────────────────────────────────────────

async def git_restore_snapshot(session: Session) -> str:
    """
    执行 git stash pop 回滚上次编辑。
    由 cli.py 的 /undo 命令调用。
    """
    if not _is_git_repo(session.project_root):
        return "当前目录不是 git 仓库"

    rc, out = await _run_git(session.project_root, "stash", "pop")
    if rc != 0:
        if "No stash entries" in out or "no stash" in out.lower():
            return "没有可回滚的 snapshot"
        return f"回滚失败: {out}"
    return f"✓ 已回滚到上一个 snapshot\n{out}"
