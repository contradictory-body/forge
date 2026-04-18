"""
bash.py — Shell 命令执行工具

bash: 在项目根目录下执行 shell 命令，带超时和输出截断。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from ..core import Session

DEFAULT_TIMEOUT = 30


async def _handler(session: Session, arguments: dict) -> ToolResult:
    command: str = arguments.get("command", "")
    timeout: int = arguments.get("timeout", DEFAULT_TIMEOUT)

    if not command:
        return ToolResult(content="缺少 command 参数", is_error=True)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=session.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return ToolResult(content=f"启动命令失败: {e}", is_error=True)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(content=f"命令超时被终止 (timeout={timeout}s)", is_error=True)

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    combined = stdout
    if stderr:
        combined = f"{stdout}\n[stderr]\n{stderr}" if stdout else stderr

    returncode = proc.returncode or 0

    if returncode != 0:
        return ToolResult(
            content=f"Exit code: {returncode}\n{combined}",
            is_error=True,
        )

    if not combined.strip():
        return ToolResult(content="✓ Command completed (no output)")

    return ToolResult(content=combined)


TOOL_DEF = ToolDefinition(
    name="bash",
    description="Execute a shell command in the project root directory.",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30).",
            },
        },
        "required": ["command"],
    },
    handler=_handler,
    readonly=False,
    max_result_chars=10000,
)
