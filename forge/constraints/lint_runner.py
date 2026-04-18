"""
lint_runner.py — 编辑后 Lint 运行器

设计核心：
- 成功时完全静默（不向对话历史添加任何内容）→ 零 token 消耗
- 失败时将错误消息注入对话历史（通过 HookResult.inject_content）
- 错误消息格式化为 agent 可直接理解和执行的修复指令
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..core import HookResult, ToolCall, ToolResult

if TYPE_CHECKING:
    from ..core import Session
    from .rules_parser import ConstraintConfig

logger = logging.getLogger("forge.constraints.lint_runner")

# 模块级状态：由 init_quality_layer 通过 set_config() 初始化
_constraint_config: ConstraintConfig | None = None


def set_config(config: ConstraintConfig) -> None:
    """由 init_quality_layer 调用，设置约束配置。"""
    global _constraint_config
    _constraint_config = config


async def lint_hook(
    session: Session,
    tool_call: ToolCall,
    tool_result: ToolResult,
) -> HookResult:
    """
    post_edit 钩子：每次 edit_file 成功后自动运行。

    无违规 → HookResult()（完全静默）
    有违规 → HookResult(inject_content=格式化错误消息)
    """
    # 编辑失败 → 不需要 lint
    if tool_result.is_error:
        return HookResult()

    if _constraint_config is None:
        return HookResult()

    file_path = tool_call.arguments.get("path", "")
    if not file_path:
        return HookResult()

    abs_path = Path(session.project_root) / file_path
    project_root = Path(session.project_root)

    all_violations: list[str] = []

    # a. 项目自带 lint
    if _constraint_config.lint.command:
        lint_output = await _run_project_lint(
            _constraint_config.lint.command, file_path, session.project_root
        )
        if lint_output:
            all_violations.append(lint_output)

    # b. Import 方向检查
    if _constraint_config.architecture.layers and abs_path.suffix == ".py":
        from .import_checker import check_file
        import_violations = check_file(
            abs_path, project_root, _constraint_config.architecture
        )
        for v in import_violations:
            all_violations.append(v.message)

    # c. 结构性检查
    from .structural_tests import check_file_structural
    structural_violations = check_file_structural(
        abs_path, project_root, _constraint_config.structural
    )
    for v in structural_violations:
        all_violations.append(v.message)

    # 无违规 → 静默
    if not all_violations:
        return HookResult()

    # 有违规 → 格式化并注入
    total_count = len(all_violations)
    violations_text = (
        f"=== Constraint Check Results ===\n"
        f"{total_count} violation(s) found after editing {file_path}:\n\n"
    )
    for v_msg in all_violations:
        violations_text += v_msg + "\n\n"
    violations_text += "Please fix these violations before proceeding.\n"
    violations_text += "=== End Constraint Check ==="

    logger.info("Lint hook: %d violations for %s", total_count, file_path)
    return HookResult(inject_content=violations_text)


async def _run_project_lint(
    lint_command: str,
    edited_file: str,
    cwd: str,
) -> str:
    """运行项目自带 linter，返回错误输出（空字符串表示通过）。"""
    # 将 lint 命令限定为被编辑的文件
    # 例如 "ruff check ." → "ruff check {edited_file}"
    cmd = lint_command.rstrip()
    if cmd.endswith("."):
        cmd = cmd[:-1] + edited_file
    else:
        cmd = f"{cmd} {edited_file}"

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            combined = f"{output}\n{err}".strip()
            return (
                f"CONSTRAINT VIOLATION [project_lint]:\n"
                f"Command: {cmd}\n"
                f"Output:\n{combined[:2000]}"
            )
    except asyncio.TimeoutError:
        return f"CONSTRAINT VIOLATION [project_lint]:\nCommand: {cmd}\nLint timed out (15s)"
    except FileNotFoundError:
        logger.debug("Lint command not found: %s", cmd)
    except Exception as e:
        logger.debug("Lint command failed: %s", e)

    return ""
