"""
tool_dispatch.py — 工具分发引擎

维护工具注册表，接收 ToolCall，经过权限检查后执行 handler。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from . import (
    ToolResult, ToolCall, ToolDefinition, Session,
    SessionMode, PermissionDecision,
)
from .permission import check_permission
from ..util.terminal_ui import ask_permission

logger = logging.getLogger("forge.core.tool_dispatch")


class ToolDispatch:
    """
    工具分发器。
    所有方法均为静态方法，状态存储在 Session.tool_registry 中。
    """

    @staticmethod
    def register(session: Session, definition: ToolDefinition) -> None:
        """注册一个工具。同名覆盖并打印警告。"""
        if definition.name in session.tool_registry:
            logger.warning("Tool '%s' already registered, overwriting", definition.name)
        session.tool_registry[definition.name] = definition
        logger.debug("Registered tool: %s (readonly=%s)", definition.name, definition.readonly)

    @staticmethod
    def register_builtin_tools(session: Session) -> None:
        """注册所有内置工具。"""
        from ..tools.read import TOOL_DEF as read_def
        from ..tools.edit import TOOL_DEF as edit_def
        from ..tools.bash import TOOL_DEF as bash_def
        from ..tools.search import TOOL_DEF as search_def
        from ..tools.git import TOOL_DEFS as git_defs
        from ..tools.glob import TOOL_DEF as glob_def
        from ..tools.features_writer import TOOL_DEF as features_writer_def

        for td in [read_def, edit_def, bash_def, search_def, glob_def, features_writer_def]:
            ToolDispatch.register(session, td)
        for td in git_defs:
            ToolDispatch.register(session, td)

        logger.info("Registered %d builtin tools", len(session.tool_registry))

    @staticmethod
    async def execute(session: Session, tool_call: ToolCall) -> ToolResult:
        """
        执行一个工具调用。

        流程：查找 → Plan Mode 检查 → 权限检查 → 执行（含重试）→ 输出截断
        """
        # 1. 查找
        tool = session.tool_registry.get(tool_call.name)
        if tool is None:
            return ToolResult(content=f"未知工具: {tool_call.name}", is_error=True)

        # 2. Plan Mode 检查
        if session.mode == SessionMode.PLAN and not tool.readonly:
            return ToolResult(
                content=f"Plan Mode 下不允许执行写操作: {tool_call.name}",
                is_error=True,
            )

        # 3. 权限检查
        decision = check_permission(session, tool_call)
        if decision == PermissionDecision.DENY:
            return ToolResult(content=f"权限拒绝: {tool_call.name}", is_error=True)
        if decision == PermissionDecision.ASK:
            if not ask_permission(tool_call):
                return ToolResult(content="用户拒绝执行", is_error=True)

        # 4. 执行（带一次重试）
        result: ToolResult
        try:
            result = await tool.handler(session, tool_call.arguments)
        except Exception as e:
            logger.warning("Tool '%s' failed (%s), retrying once...", tool_call.name, e)
            try:
                result = await tool.handler(session, tool_call.arguments)
            except Exception as e2:
                result = ToolResult(
                    content=f"工具执行失败（已重试）: {type(e2).__name__}: {e2}",
                    is_error=True,
                )

        # 5. 输出截断
        if len(result.content) > tool.max_result_chars:
            _truncate_and_save(session, tool_call.name, result, tool.max_result_chars)

        return result


def _truncate_and_save(
    session: Session,
    tool_name: str,
    result: ToolResult,
    max_chars: int,
) -> None:
    """将超长输出写入文件，截断 result.content。"""
    output_dir = Path(session.forge_dir) / "tool_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    file_path = output_dir / f"{tool_name}_{file_id}.txt"
    file_path.write_text(result.content, encoding="utf-8")
    result.content = (
        result.content[:max_chars]
        + f"\n\n[Output truncated. Full content saved to: {file_path}]"
    )
    logger.debug("Truncated output for %s, saved to %s", tool_name, file_path)
