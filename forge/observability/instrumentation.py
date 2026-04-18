"""
instrumentation.py — 非侵入式监测点装配

通过 monkey-patch 已有的可替换钩子点收集事件，
不修改 loop.py / tool_dispatch.py 的源码。

为什么这种做法合法：
- Forge 本身就是通过 `loop.build_system_prompt = assembler.build`
  这种模式让 context 层替换默认实现
- 我们只是在已被 context 层替换后的版本上再包一层
- 所有包装都是 "捕获 → 转发 → 记录"，不改变语义
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session
    from .tracer import Tracer

logger = logging.getLogger("forge.observability.instrumentation")


# ── Layer-stat 收集 ──────────────────────────────────────────────────────

def wrap_build_system_prompt(tracer: Tracer) -> None:
    """
    包装 loop.build_system_prompt，追踪每次 prompt 构建的总长度。
    更精细的 per-layer 统计需要修改 assembler.build，不在本次升级范围内。
    """
    from ..core import loop as loop_module

    original = loop_module.build_system_prompt

    def traced_build(session):  # noqa: ANN001
        t0 = time.time()
        try:
            result = original(session)
        except Exception:
            raise
        tracer.emit_timed("prompt_built", time.time() - t0, {
            "total_chars": len(result),
            "estimated_tokens": len(result) // 4,
            "has_forge_md": bool(session.forge_md_content),
            "has_session_memory": bool(session.session_memory_content),
            "has_progress": session.progress_data is not None,
        })
        return result

    loop_module.build_system_prompt = traced_build


# ── LLM 调用耗时与 token 消耗 ────────────────────────────────────────────

def wrap_query_llm(tracer: Tracer) -> None:
    """
    包装 loop.query_llm。这里要特别注意：
    loop.py 顶部 `from .query import query_llm` 把引用绑到了 loop 模块的
    命名空间，所以我们必须 patch `loop.query_llm`，而不是 `query.query_llm`。
    """
    from ..core import loop as loop_module

    original = loop_module.query_llm

    async def traced_query(**kwargs):
        t0 = time.time()
        tool_count = len(kwargs.get("tool_definitions", []))
        message_count = len(kwargs.get("messages", []))
        try:
            response = await original(**kwargs)
            tracer.emit_timed("llm_call", time.time() - t0, {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "tool_calls_returned": len(response.tool_calls),
                "text_chars": len(response.text),
                "context_messages": message_count,
                "context_tools": tool_count,
            })
            return response
        except Exception as e:
            tracer.emit_timed("llm_call_error", time.time() - t0, {
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            })
            raise

    loop_module.query_llm = traced_query  # type: ignore[assignment]


# ── 工具执行追踪 ─────────────────────────────────────────────────────────

def wrap_tool_dispatch(tracer: Tracer) -> None:
    """包装 ToolDispatch.execute 追踪每次工具调用。"""
    from ..core import tool_dispatch as td_module
    from ..core.permission import check_permission

    original_execute = td_module.ToolDispatch.execute

    async def traced_execute(session, tool_call):  # noqa: ANN001
        # 预计算权限决策（execute 内部也会算一次，代价可忽略）
        try:
            decision = check_permission(session, tool_call).value
        except Exception:
            decision = "unknown"

        t0 = time.time()
        result = await original_execute(session, tool_call)
        tracer.emit_timed("tool_call", time.time() - t0, {
            "tool": tool_call.name,
            "id": tool_call.id,
            "is_error": result.is_error,
            "content_len": len(result.content),
            "permission": decision,
            "args_keys": list(tool_call.arguments.keys()),
        })
        return result

    td_module.ToolDispatch.execute = staticmethod(traced_execute)


# ── Compaction 追踪 ─────────────────────────────────────────────────────

def wrap_compact_callback(session: Session, tracer: Tracer) -> None:
    """
    包装 session.compact_callback。必须在 init_context_layer 之后调用，
    否则原 callback 还是 None。
    """
    if session.compact_callback is None:
        logger.debug("No compact_callback to wrap")
        return

    original = session.compact_callback

    async def traced_compact(s):  # noqa: ANN001
        def _safe_ratio(sess):
            try:
                return round(sess.token_usage_ratio, 3)
            except Exception:
                return None
        ratio_before = _safe_ratio(s)
        msg_before = len(s.messages)
        t0 = time.time()
        try:
            await original(s)
        except Exception as e:
            tracer.emit_timed("compaction_error", time.time() - t0, {
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            })
            raise
        tracer.emit_timed("compaction", time.time() - t0, {
            "ratio_before": ratio_before,
            "ratio_after": _safe_ratio(s),
            "messages_before": msg_before,
            "messages_after": len(s.messages),
            "compaction_count": s.compaction_count,
        })

    session.compact_callback = traced_compact


# ── Evaluator 追踪 ──────────────────────────────────────────────────────

def wrap_evaluator(tracer: Tracer) -> None:
    """
    包装 evaluation.evaluator.evaluate 函数。
    这是捕获评审结果的关键——每次 feature 评审都会产生一条 evaluator_verdict 事件，
    长期 trend 分析就靠它。
    """
    try:
        from ..evaluation import evaluator as ev_module
    except ImportError:
        logger.debug("evaluation module not available")
        return

    original = ev_module.evaluate

    async def traced_evaluate(session, eval_input, criteria):  # noqa: ANN001
        t0 = time.time()
        result = await original(session, eval_input, criteria)
        tracer.emit_timed("evaluator_verdict", time.time() - t0, {
            "verdict": result.get("verdict"),
            "score": result.get("score"),
            "criteria_results": [
                {
                    "name": c.get("name"),
                    "score": c.get("score"),
                    "passed": c.get("passed"),
                }
                for c in (result.get("criteria_results") or [])
                if isinstance(c, dict)
            ],
            "input_chars": len(eval_input),
        })
        return result

    ev_module.evaluate = traced_evaluate


# ── 每轮开始/结束 + 生命周期 ─────────────────────────────────────────────

def install_turn_hooks(session: Session, tracer: Tracer) -> None:
    """注册 pre_query 和 on_session_end 钩子发射对应事件。"""
    from ..core import HookResult

    turn_counter = {"n": 0}

    async def pre_query_hook(sess):  # noqa: ANN001
        turn_counter["n"] += 1
        try:
            ratio = round(sess.token_usage_ratio, 3)
        except Exception:
            ratio = None
        tracer.emit("turn_start", {
            "turn": turn_counter["n"],
            "messages_count": len(sess.messages),
            "iteration_count": sess.iteration_count,
            "token_ratio": ratio,
        })

    session.hooks.setdefault("pre_query", []).append(pre_query_hook)

    async def pre_tool_hook(sess, tool_call):  # noqa: ANN001
        # 在 wrap_tool_dispatch 之外额外发射一个 start 事件
        # 方便 analyzer 把 start/end 配对
        tracer.emit("tool_call_start", {
            "tool": tool_call.name,
            "id": tool_call.id,
        })
        return HookResult()

    session.hooks.setdefault("pre_tool", []).append(pre_tool_hook)

    async def on_session_end_hook(sess):  # noqa: ANN001
        # token_usage_ratio 可能因 tiktoken 加载失败而抛异常 — 防御性处理
        try:
            ratio = round(sess.token_usage_ratio, 3)
        except Exception:
            ratio = None
        tracer.emit("session_summary", {
            "iterations": sess.iteration_count,
            "input_tokens": sess.total_input_tokens,
            "output_tokens": sess.total_output_tokens,
            "compaction_count": sess.compaction_count,
            "final_token_ratio": ratio,
            "total_messages": len(sess.messages),
        })
        tracer.close()

    # on_session_end 是在最后调用的，我们追加到末尾
    session.hooks.setdefault("on_session_end", []).append(on_session_end_hook)
