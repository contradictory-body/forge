"""
forge.constraints — 架构约束引擎 + 质量系统初始化入口

提供 init_quality_layer() 供 cli.py 调用，完成：
1. 解析 FORGE.md 中的约束规则
2. 注册 post_edit lint 钩子
3. 注册 on_feature_complete evaluator 钩子
4. 注册 mark_feature_complete 工具
5. 发现并注册 MCP 工具
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.constraints")


def init_quality_layer(session: Session) -> None:
    """
    由 cli.py 在 create_session 末尾调用（在 init_context_layer 之后）。
    """
    from .rules_parser import parse_constraints
    from .lint_runner import set_config, lint_hook
    from ..evaluation.feedback_loop import evaluate_hook, register_mark_complete_tool
    from ..harness.hooks import register_quality_hooks

    # 1. 解析约束规则（优先使用已编译的结果）
    compiled = getattr(session, "forge_md_compiled", None)
    if compiled is not None:
        config = compiled.to_constraint_config()
    else:
        config = parse_constraints(session.forge_md_content)
    set_config(config)
    logger.debug(
        "Constraint config: %d layers, lint=%s",
        len(config.architecture.layers),
        bool(config.lint.command),
    )

    # 2. 注册钩子
    register_quality_hooks(
        session,
        post_edit=lint_hook,
        on_feature_complete=evaluate_hook,
    )

    # 3. 注册 mark_feature_complete 工具
    register_mark_complete_tool(session)

    # 4. MCP 工具发现（异步，在事件循环中运行）
    _try_discover_mcp(session)

    logger.info("Quality layer initialized")


def _try_discover_mcp(session: Session) -> None:
    """尝试发现并注册 MCP 工具。"""
    from .rules_parser import parse_constraints
    import re

    # 从 FORGE.md 中提取 mcp_servers 配置
    if not session.forge_md_content:
        return

    try:
        import yaml
    except ImportError:
        return

    yaml_blocks = re.findall(r"```yaml\s*\n(.*?)```", session.forge_md_content, re.DOTALL)
    mcp_configs: list[dict] = []
    for block in yaml_blocks:
        try:
            data = yaml.safe_load(block)
        except Exception:
            continue
        if isinstance(data, dict) and "mcp_servers" in data:
            servers = data["mcp_servers"]
            if isinstance(servers, list):
                mcp_configs.extend(servers)

    if not mcp_configs:
        return

    try:
        from ..mcp.registry import discover_and_register
        # 在后台运行 MCP 发现
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(discover_and_register(session, mcp_configs))
        else:
            loop.run_until_complete(discover_and_register(session, mcp_configs))
    except Exception as e:
        logger.warning("MCP discovery failed: %s", e)
