"""
structural_tests.py — 结构性约束检查

检查项：文件行数、函数长度、文件命名、禁止 pattern。
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rules_parser import StructuralRule

logger = logging.getLogger("forge.constraints.structural_tests")

SNAKE_CASE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass
class StructuralViolation:
    """一条结构性违规。"""
    file: str
    line: int | None
    rule: str
    message: str


def check_file_structural(
    file_path: Path,
    project_root: Path,
    rules: StructuralRule,
) -> list[StructuralViolation]:
    """对单个文件执行结构性检查。"""
    violations: list[StructuralViolation] = []

    try:
        rel_path = str(file_path.relative_to(project_root))
    except ValueError:
        rel_path = str(file_path)

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return []

    lines = content.splitlines()
    line_count = len(lines)

    # 1. 文件行数
    if line_count > rules.max_file_lines:
        violations.append(StructuralViolation(
            file=rel_path,
            line=None,
            rule="file_size",
            message=(
                f"CONSTRAINT VIOLATION [file_size]:\n"
                f"File: {rel_path} ({line_count} lines, limit: {rules.max_file_lines})\n"
                f"Fix: Split this file into smaller modules. Consider extracting related "
                f"functions into a separate file."
            ),
        ))

    # 2. 函数长度（仅 .py 文件）
    if file_path.suffix == ".py":
        try:
            tree = ast.parse(content, filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.body:
                        continue
                    first_line = node.body[0].lineno
                    last_line = node.body[-1].end_lineno or node.body[-1].lineno
                    body_lines = last_line - first_line + 1
                    if body_lines > rules.max_function_lines:
                        violations.append(StructuralViolation(
                            file=rel_path,
                            line=node.lineno,
                            rule="function_size",
                            message=(
                                f"CONSTRAINT VIOLATION [function_size]:\n"
                                f"File: {rel_path}, Function: {node.name} "
                                f"({body_lines} lines, limit: {rules.max_function_lines})\n"
                                f"Fix: Break this function into smaller helper functions."
                            ),
                        ))
        except SyntaxError:
            pass

    # 3. 文件命名
    if rules.naming_files == "snake_case":
        stem = file_path.stem
        if stem != "__init__" and not SNAKE_CASE_RE.match(stem):
            violations.append(StructuralViolation(
                file=rel_path,
                line=None,
                rule="naming_files",
                message=(
                    f"CONSTRAINT VIOLATION [naming_files]:\n"
                    f"File: {rel_path}\n"
                    f"Violation: File name '{stem}' does not follow snake_case convention\n"
                    f"Fix: Rename to {stem.lower().replace('-', '_')}{file_path.suffix}"
                ),
            ))

    # 4. 禁止 pattern
    file_name = file_path.name
    for rule_dict in rules.forbidden_patterns:
        pattern = rule_dict.get("pattern", "")
        excludes = rule_dict.get("exclude", []) or []
        msg_template = rule_dict.get("message", f"Forbidden pattern: {pattern}")

        if file_name in excludes:
            continue
        if not pattern:
            continue

        try:
            compiled = re.compile(pattern)
        except re.error:
            continue

        for line_no, line_text in enumerate(lines, start=1):
            if compiled.search(line_text):
                violations.append(StructuralViolation(
                    file=rel_path,
                    line=line_no,
                    rule="forbidden_pattern",
                    message=(
                        f"CONSTRAINT VIOLATION [forbidden_pattern]:\n"
                        f"File: {rel_path}, Line {line_no}\n"
                        f"Matched: {pattern}\n"
                        f"Fix: {msg_template}"
                    ),
                ))
                break  # 每个 pattern 每个文件只报一次

    return violations
