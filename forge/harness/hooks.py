"""
hooks.py — 统一钩子注册

此文件是 Dev B 和 Dev C 所有钩子的注册中心。
Dev C 通过 register_quality_hooks() 追加自己的钩子。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session, ToolCall, ToolResult, HookResult

logger = logging.getLogger("forge.harness.hooks")


def register_lifecycle_hooks(session: Session) -> None:
    """
    注册 Dev B 负责的所有生命周期钩子。
    由 init_context_layer() 调用。
    """
    from .handoff import on_session_end
    from ..context.session_memory import on_pre_compact, maybe_extract

    # 1. on_session_end → handoff
    session.hooks["on_session_end"].append(on_session_end)

    # 2. pre_compact → session_memory
    session.hooks["pre_compact"].append(on_pre_compact)

    # 3. post_edit / post_bash → session memory 计数器
    #    这个钩子仅调用 maybe_extract，不注入内容（返回空 HookResult）
    async def _session_memory_counter_hook(
        sess: Session,
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> HookResult:
        """触发 session memory 提取计数器，不注入任何内容。"""
        from ..core import HookResult as HR
        try:
            await maybe_extract(sess)
        except Exception as e:
            logger.debug("session memory counter hook error: %s", e)
        return HR()  # 空结果：不注入内容，不中止

    session.hooks["post_edit"].append(_session_memory_counter_hook)
    session.hooks["post_bash"].append(_session_memory_counter_hook)

    logger.debug(
        "Lifecycle hooks registered: on_session_end, pre_compact, "
        "post_edit/post_bash (session memory counter)"
    )


def register_quality_hooks(session: Session, **kwargs) -> None:
    """
    预留给 Dev C 调用的注册入口。

    Dev C 调用方式：
        register_quality_hooks(
            session,
            post_edit=constraints.lint_hook,
            on_feature_complete=evaluation.evaluate_hook,
        )

    此函数将传入的钩子追加到 session.hooks 对应列表中。
    """
    for hook_name, hook_fn in kwargs.items():
        if hook_name in session.hooks:
            session.hooks[hook_name].append(hook_fn)
            logger.debug("Quality hook registered: %s", hook_name)
        else:
            logger.warning(
                "Unknown hook name '%s' passed to register_quality_hooks, ignoring",
                hook_name,
            )
