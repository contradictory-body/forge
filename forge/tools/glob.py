"""
glob.py — 目录树浏览工具

list_directory: 递归显示目录树结构，排除常见无关目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from ..core import Session

EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".forge", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".egg-info",
}

MAX_ENTRIES = 200


def _build_tree(
    root: Path,
    prefix: str,
    depth: int,
    max_depth: int,
    entries: list[str],
    count: list[int],
) -> None:
    """递归构建目录树文本。"""
    if depth > max_depth or count[0] >= MAX_ENTRIES:
        return

    try:
        items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return

    # 过滤
    items = [
        p for p in items
        if p.name not in EXCLUDE_DIRS and not p.name.startswith(".")
        or (p.name.startswith(".") and p.name in (".env.example", ".gitignore"))
    ]

    for i, path in enumerate(items):
        if count[0] >= MAX_ENTRIES:
            return

        is_last = (i == len(items) - 1)
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")

        if path.is_dir():
            entries.append(f"{prefix}{connector}{path.name}/")
            count[0] += 1
            _build_tree(path, child_prefix, depth + 1, max_depth, entries, count)
        else:
            entries.append(f"{prefix}{connector}{path.name}")
            count[0] += 1


async def _handler(session: Session, arguments: dict) -> ToolResult:
    rel_path: str = arguments.get("path", "")
    max_depth: int = arguments.get("depth", 2)

    target = Path(session.project_root)
    if rel_path:
        target = (target / rel_path).resolve()

    root_resolved = Path(session.project_root).resolve()
    if not str(target.resolve()).startswith(str(root_resolved)):
        return ToolResult(content="路径超出项目范围", is_error=True)

    if not target.exists():
        return ToolResult(content=f"目录不存在: {rel_path or '.'}", is_error=True)
    if not target.is_dir():
        return ToolResult(content=f"路径不是目录: {rel_path}", is_error=True)

    entries: list[str] = []
    count = [0]

    try:
        dir_name = target.relative_to(root_resolved)
    except ValueError:
        dir_name = target.name
    entries.append(f"{dir_name}/")
    count[0] += 1

    _build_tree(target, "", 0, max_depth, entries, count)

    total_on_disk = sum(1 for _ in target.rglob("*"))
    result = "\n".join(entries)
    if count[0] >= MAX_ENTRIES:
        result += f"\n[truncated, showing {MAX_ENTRIES} of {total_on_disk} entries]"

    return ToolResult(content=result)


TOOL_DEF = ToolDefinition(
    name="list_directory",
    description="Show directory tree structure up to a specified depth.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to project root (default: root).",
            },
            "depth": {
                "type": "integer",
                "description": "Max depth to recurse (default 2).",
            },
        },
    },
    handler=_handler,
    readonly=True,
)
