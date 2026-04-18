"""
client.py — MCP Client

连接外部 MCP Server，支持 stdio 和 SSE 传输。
连接失败时 yield None（不抛异常）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

logger = logging.getLogger("forge.mcp.client")


class MCPClient:
    """MCP 客户端。支持 stdio 和 SSE 传输。"""

    def __init__(self, server_config: dict):
        self.name: str = server_config.get("name", "unknown")
        self.command: str = server_config.get("command", "")
        self.args: list[str] = server_config.get("args", [])
        self.url: str = server_config.get("url", "")
        self._transport = "sse" if self.url else "stdio"

    @asynccontextmanager
    async def connect(self):
        """连接到 MCP server。连接失败时 yield None。"""
        try:
            if self._transport == "stdio":
                async with self._connect_stdio() as session:
                    yield session
            else:
                async with self._connect_sse() as session:
                    yield session
        except ImportError:
            logger.warning("mcp package not installed for server '%s'", self.name)
            yield None
        except Exception as e:
            logger.warning("MCP [%s] connection failed: %s", self.name, e)
            yield None

    @asynccontextmanager
    async def _connect_stdio(self):
        from mcp.client.stdio import stdio_client
        from mcp import ClientSession
        async with stdio_client(command=self.command, args=self.args) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                logger.info("MCP [%s] connected via stdio", self.name)
                yield session

    @asynccontextmanager
    async def _connect_sse(self):
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        async with sse_client(self.url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                logger.info("MCP [%s] connected via SSE", self.name)
                yield session
