"""
forge.context — 上下文工程与生命周期管理

提供 init_context_layer() 作为 Dev B 所有功能的统一初始化入口，
由 cli.py 在 create_session() 末尾调用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.context")


def init_context_layer(session: Session) -> None:
    """
    初始化上下文层。由 cli.py 在 create_session 末尾调用。

    步骤：
    1. 替换 loop.build_system_prompt → assembler.build
    2. 注册 session.compact_callback → compaction.compact
    3. 注册生命周期钩子（on_session_end, pre_compact, session memory 计数器）
    4. 注册 pre_query 钩子（刷新 git 状态缓存）
    5. 运行启动时熵检测与清理
    """
    from ..core import loop as loop_module
    from .assembler import build as assembler_build, refresh_git_status
    from .compaction import compact
    from .session_memory import on_pre_compact
    from ..harness.hooks import register_lifecycle_hooks
    from ..harness.entropy import startup_check

    # 1. 替换 system prompt 构建函数
    loop_module.build_system_prompt = assembler_build
    logger.debug("Replaced build_system_prompt with assembler.build")

    # 2. 注册 compaction 回调
    session.compact_callback = compact
    logger.debug("Registered compact_callback")

    # 3. 注册生命周期钩子
    register_lifecycle_hooks(session)

    # 4. 注册 pre_query 钩子刷新 git 状态
    async def _refresh_git_hook(sess: Session) -> None:
        await refresh_git_status(sess)

    session.hooks.setdefault("pre_query", []).append(_refresh_git_hook)
    logger.debug("Registered pre_query git status refresh hook")

    # 5. 启动时熵检测
    startup_check(session)

    logger.info("Context layer initialized")
