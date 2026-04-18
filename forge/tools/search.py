"""
search.py — 代码搜索工具

search_code: ripgrep 搜索，不可用时回退到 grep -rn。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.tools.search")

_HAS_RG = shutil.which("rg") is not None


async def _search_rg(
    pattern: str, search_path: str, file_pattern: str | None, max_count: int = 50,
) -> tuple[str, bool]:
    """使用 ripgrep 搜索，返回 (输出, 是否成功)。"""
    cmd = ["rg", "--json", "--max-count", str(max_count), pattern, search_path]
    if file_pattern:
        cmd.extend(["--glob", file_pattern])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    output = stdout.decode("utf-8", errors="replace")

    matches: list[str] = []
    root = Path(search_path).resolve()
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data", {})
        abs_path = data.get("path", {}).get("text", "")
        line_no = data.get("line_number", 0)
        line_text = data.get("lines", {}).get("text", "").rstrip()
        try:
            rel = str(Path(abs_path).relative_to(root))
        except ValueError:
            rel = abs_path
        matches.append(f"{rel}:{line_no}: {line_text}")

    return "\n".join(matches), True


async def _search_grep(
    pattern: str, search_path: str, file_pattern: str | None,
) -> tuple[str, bool]:
    """回退到 grep -rn。"""
    cmd = f"grep -rn --include='{'*' if not file_pattern else file_pattern}' '{pattern}' '{search_path}'"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    return stdout.decode("utf-8", errors="replace").strip(), True


async def _handler(session: Session, arguments: dict) -> ToolResult:
    pattern: str = arguments.get("pattern", "")
    path: str = arguments.get("path", "")
    file_pattern: str | None = arguments.get("file_pattern")

    if not pattern:
        return ToolResult(content="缺少 pattern 参数", is_error=True)

    search_path = str(Path(session.project_root) / path) if path else session.project_root

    try:
        if _HAS_RG:
            output, _ = await _search_rg(pattern, search_path, file_pattern)
        else:
            output, _ = await _search_grep(pattern, search_path, file_pattern)
    except asyncio.TimeoutError:
        return ToolResult(content="搜索超时", is_error=True)
    except Exception as e:
        return ToolResult(content=f"搜索失败: {e}", is_error=True)

    if not output.strip():
        return ToolResult(content="No matches found")

    return ToolResult(content=output)


TOOL_DEF = ToolDefinition(
    name="search_code",
    description="Search for a regex pattern across files using ripgrep (or grep fallback).",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Optional directory or file path to limit search scope.",
            },
            "file_pattern": {
                "type": "string",
                "description": "Optional file glob filter (e.g. '*.py').",
            },
        },
        "required": ["pattern"],
    },
    handler=_handler,
    readonly=True,
    max_result_chars=15000,
)
