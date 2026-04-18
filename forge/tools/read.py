"""
read.py — 文件读取工具

read_file: 读取项目内文件，带行号，支持行范围。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from ..core import Session


def _resolve_safe(project_root: str, rel_path: str) -> tuple[Path | None, str]:
    """解析路径并做安全检查。返回 (绝对路径, 错误信息)。"""
    root = Path(project_root).resolve()
    target = (root / rel_path).resolve()
    if not str(target).startswith(str(root)):
        return None, "路径超出项目范围"
    return target, ""


async def _handler(session: Session, arguments: dict) -> ToolResult:
    rel_path: str = arguments.get("path", "")
    line_range: list[int] | None = arguments.get("line_range")

    if not rel_path:
        return ToolResult(content="缺少 path 参数", is_error=True)

    target, err = _resolve_safe(session.project_root, rel_path)
    if err:
        return ToolResult(content=err, is_error=True)
    assert target is not None

    if not target.exists():
        return ToolResult(content=f"文件不存在: {rel_path}", is_error=True)
    if target.is_dir():
        return ToolResult(content=f"路径是目录而非文件: {rel_path}", is_error=True)

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(content=f"文件不是有效的 UTF-8 文本: {rel_path}", is_error=True)
    except Exception as e:
        return ToolResult(content=f"读取失败: {e}", is_error=True)

    lines = content.splitlines(keepends=True)

    if line_range and len(line_range) == 2:
        start, end = line_range
        start = max(1, start)
        end = min(len(lines), end)
        lines = lines[start - 1: end]
        start_offset = start
    else:
        start_offset = 1

    numbered = []
    for i, line in enumerate(lines):
        line_no = start_offset + i
        numbered.append(f"{line_no:4d} | {line.rstrip()}")

    result_text = "\n".join(numbered)

    # 更新 recently_read_files
    abs_str = str(target)
    if abs_str in session.recently_read_files:
        session.recently_read_files.remove(abs_str)
    session.recently_read_files.insert(0, abs_str)
    session.recently_read_files = session.recently_read_files[:5]

    return ToolResult(content=result_text)


TOOL_DEF = ToolDefinition(
    name="read_file",
    description="Read a file's contents with line numbers. Supports optional line range.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the project root.",
            },
            "line_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "Optional [start_line, end_line], 1-based.",
            },
        },
        "required": ["path"],
    },
    handler=_handler,
    readonly=True,
    max_result_chars=30000,
)
