"""
import_checker.py — Python Import 方向静态检查

层级从底到顶：layers = [types, config, repository, service, handler]
低层 import 高层 → 违规（例如 repository import handler）
cross_cutting 模块不受限。
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rules_parser import ArchitectureRules

logger = logging.getLogger("forge.constraints.import_checker")

EXCLUDE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".forge"}


@dataclass
class ImportViolation:
    """一条 import 违规。"""
    file: str
    line: int
    importing_layer: str
    imported_module: str
    imported_layer: str
    message: str


def _detect_layer(path: Path, project_root: Path, rules: ArchitectureRules) -> str | None:
    """检测文件所在的层级。返回层名或 None。"""
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    # 匹配第一级目录名
    top_dir = parts[0]
    if top_dir in rules.layers:
        return top_dir
    # 也检查第二级（如 src/handler/...）
    if len(parts) > 1 and parts[1] in rules.layers:
        return parts[1]
    return None


def _extract_imported_module_top(node: ast.Import | ast.ImportFrom) -> list[str]:
    """从 import 节点提取被导入模块的顶级名称。"""
    names: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            names.append(alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            names.append(node.module.split(".")[0])
    return names


def check_file(
    file_path: Path,
    project_root: Path,
    rules: ArchitectureRules,
) -> list[ImportViolation]:
    """检查单个 Python 文件的 import 方向。"""
    if not rules.layers:
        return []

    importing_layer = _detect_layer(file_path, project_root, rules)
    if importing_layer is None:
        return []

    importing_index = rules.layers.index(importing_layer)

    try:
        source = file_path.read_text(encoding="utf-8")
    except Exception:
        return []

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    violations: list[ImportViolation] = []
    # 严格邻接规则：每层只能 import 紧邻的下一层，不能跳层
    # handler(4) → service(3) OK; handler(4) → repository(2) VIOLATION（跳过 service）
    if importing_index > 0:
        allowed_layers = [rules.layers[importing_index - 1]]
    else:
        allowed_layers = []  # 最底层不能 import 任何其他层

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for mod_top in _extract_imported_module_top(node):
            # cross_cutting → skip
            if mod_top in rules.cross_cutting:
                continue
            # same layer → skip
            if mod_top == importing_layer:
                continue
            # not a known layer → skip
            if mod_top not in rules.layers:
                continue
            # 违规：导入了非邻接层
            if mod_top not in allowed_layers:
                try:
                    rel_path = str(file_path.relative_to(project_root))
                except ValueError:
                    rel_path = str(file_path)

                layer_chain = " → ".join(reversed(rules.layers))
                violations.append(ImportViolation(
                    file=rel_path,
                    line=getattr(node, "lineno", 0),
                    importing_layer=importing_layer,
                    imported_module=mod_top,
                    imported_layer=mod_top,
                    message=(
                        f"CONSTRAINT VIOLATION [import_direction]:\n"
                        f"File: {rel_path}, Line {getattr(node, 'lineno', '?')}\n"
                        f"Violation: {importing_layer} layer imports from '{mod_top}' layer\n"
                        f"Rule: {layer_chain} (defined in FORGE.md)\n"
                        f"Fix: {importing_layer} should only import from "
                        f"{', '.join(allowed_layers)}. "
                        f"Consider adding an interface in the "
                        f"{rules.layers[importing_index - 1] if importing_index > 0 else importing_layer} layer."
                    ),
                ))

    return violations


def check_project(
    project_root: Path,
    rules: ArchitectureRules,
) -> list[ImportViolation]:
    """检查整个项目所有 .py 文件。"""
    violations: list[ImportViolation] = []
    root = project_root.resolve()
    for py_file in root.rglob("*.py"):
        # 排除无关目录
        if any(part in EXCLUDE_DIRS for part in py_file.relative_to(root).parts):
            continue
        violations.extend(check_file(py_file, root, rules))
    return violations
