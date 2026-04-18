"""
registry.py — MCP 工具发现与注册

连接 MCP Server，发现可用工具，注册到 session.tool_registry。
每次工具调用时重新连接（简单实现，MCP 调用频率不高）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult
from ..core.tool_dispatch import ToolDispatch
from .client import MCPClient

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.mcp.registry")


async def discover_and_register(session: Session, mcp_configs: list[dict]) -> int:
    """
    发现并注册所有 MCP Server 的工具。
    返回注册的工具总数。
    """
    total_registered = 0

    for server_config in mcp_configs:
        server_name = server_config.get("name", "unknown")
        client = MCPClient(server_config)

        try:
            async with client.connect() as mcp_session:
                if mcp_session is None:
                    continue

                tools_response = await mcp_session.list_tools()
                tools = getattr(tools_response, "tools", [])

                for tool in tools:
                    tool_name = f"mcp__{server_name}__{tool.name}"
                    description = f"[MCP:{server_name}] {tool.description or tool.name}"
                    input_schema = (
                        tool.inputSchema
                        if hasattr(tool, "inputSchema")
                        else {"type": "object", "properties": {}}
                    )

                    # 为每个工具创建闭包 handler
                    handler = _make_mcp_handler(server_config, tool.name)

                    tool_def = ToolDefinition(
                        name=tool_name,
                        description=description,
                        parameters=input_schema,
                        handler=handler,
                        readonly=False,
                    )
                    ToolDispatch.register(session, tool_def)
                    total_registered += 1

                logger.info(
                    "MCP [%s] discovered %d tools", server_name, len(tools)
                )
        except Exception as e:
            logger.warning("MCP [%s] discovery failed: %s", server_name, e)

    logger.info("MCP total registered: %d tools", total_registered)
    return total_registered


def _make_mcp_handler(server_config: dict, tool_name: str):
    """创建 MCP 工具的 handler 闭包。每次调用时重新连接。"""

    async def handler(session: Session, arguments: dict) -> ToolResult:
        client = MCPClient(server_config)
        try:
            async with client.connect() as mcp_session:
                if mcp_session is None:
                    return ToolResult(
                        content=f"MCP server '{server_config.get('name')}' 连接失败",
                        is_error=True,
                    )
                result = await mcp_session.call_tool(tool_name, arguments)
                # 提取文本内容
                texts = []
                for block in result.content:
                    if hasattr(block, "text"):
                        texts.append(block.text)
                    else:
                        texts.append(str(block))
                return ToolResult(content="\n".join(texts) if texts else str(result))
        except Exception as e:
            return ToolResult(
                content=f"MCP tool '{tool_name}' 调用失败: {e}",
                is_error=True,
            )

    return handler
