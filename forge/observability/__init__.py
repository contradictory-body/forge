"""
forge.observability — Observation & Evaluation-as-a-process layer

提供 init_observability_layer() 作为统一初始化入口，
由 cli.py 在 init_context_layer / init_quality_layer 之后调用。

设计原则：
1. 纯增量、非侵入：不修改 loop.py / tool_dispatch 的内部逻辑
2. 通过 monkey-patch 已有的可替换钩子点收集事件
3. 所有事件落盘为 JSONL（.forge/traces/{session_id}.jsonl）
4. 启动时检查未处理的长期问题（漂移检测），警告式提示
5. 启动时异步触发经验提炼与合并（不阻塞启动）
6. 失败时降级为 no-op（不影响 agent 正常运行）
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.observability")


def init_observability_layer(session: Session) -> None:
    """
    初始化 observability 层。必须在 init_context_layer 和
    init_quality_layer 之后调用，以便能包装它们注册的回调。

    步骤：
    1. 创建 Tracer 并挂到 session.tracer
    2. 包装 build_system_prompt → 追踪 prompt 构建
    3. 包装 loop.query_llm → 追踪每次 LLM 调用
    4. 包装 ToolDispatch.execute → 追踪每次工具执行
    5. 包装 session.compact_callback → 追踪 compaction
    6. 包装 evaluator.evaluate → 追踪评审结果
    7. 注册 pre_query 钩子发射 turn_start 事件
    8. 注册 on_session_end 钩子落盘最终统计
    9. 启动时运行漂移检测
    10. 异步触发经验提炼与合并任务
    """
    from .tracer import Tracer
    from .instrumentation import (
        wrap_build_system_prompt,
        wrap_query_llm,
        wrap_tool_dispatch,
        wrap_compact_callback,
        wrap_evaluator,
        install_turn_hooks,
    )
    from .analyzer import startup_drift_check

    # 1. 创建 tracer
    tracer = Tracer(session.forge_dir)
    session.tracer = tracer  # type: ignore[attr-defined]
    logger.debug("Tracer created: %s", tracer.trace_file)

    # 2-6. 装配 instrumentation
    wrap_build_system_prompt(tracer)
    wrap_query_llm(tracer)
    wrap_tool_dispatch(tracer)
    wrap_compact_callback(session, tracer)
    wrap_evaluator(tracer)

    # 7-8. 注册钩子
    install_turn_hooks(session, tracer)

    # 9. 启动时漂移检测
    try:
        startup_drift_check(session)
    except Exception as e:
        logger.debug("Drift check failed: %s", e)

    # 10. 异步触发经验系统任务（不阻塞启动）
    _schedule_experience_tasks(session)

    # 发射 session_start 事件
    tracer.emit("session_start", {
        "mode": session.mode.value,
        "model": session.model,
        "project_root": session.project_root,
        "tool_count": len(session.tool_registry),
    })

    logger.info("Observability layer initialized (session_id=%s)", tracer.session_id)


def _schedule_experience_tasks(session: Session) -> None:
    """
    在当前事件循环中异步调度经验提炼和合并任务。
    失败时静默跳过，不影响启动。
    """
    from .experience import startup_experience_tasks

    async def _run() -> None:
        try:
            await startup_experience_tasks(session)
        except Exception as e:
            logger.debug("Experience tasks failed: %s", e)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_run())
        else:
            # 理论上 init 时 loop 应该已经在运行（main 是 async 的）
            logger.debug("Event loop not running, skipping experience tasks")
    except Exception as e:
        logger.debug("Could not schedule experience tasks: %s", e)
