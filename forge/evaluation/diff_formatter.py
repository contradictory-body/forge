"""
diff_formatter.py — 将代码变更格式化为 Evaluator 输入

构建 Evaluator 需要的完整输入文本：feature 信息 + git diff + 测试结果 + lint 结果。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.evaluation.diff_formatter")

MAX_DIFF_CHARS = 8000


def format_eval_input(
    session: Session,
    feature: dict,
    test_output: str,
    lint_output: str,
) -> str:
    """构建 Evaluator 的输入文本。"""
    parts: list[str] = []

    # Feature info
    parts.append(f"## Feature Under Review")
    parts.append(f"ID: {feature.get('id', 'N/A')}")
    parts.append(f"Title: {feature.get('title', 'N/A')}")
    parts.append("")

    # Acceptance criteria
    parts.append("## Acceptance Criteria")
    for i, criterion in enumerate(feature.get("acceptance_criteria", []), 1):
        parts.append(f"{i}. {criterion}")
    parts.append("")

    # Git diff
    parts.append("## Code Changes (git diff)")
    diff = _get_git_diff_sync(session.project_root)
    if diff:
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
        parts.append(diff)
    else:
        parts.append("(no uncommitted changes)")
    parts.append("")

    # Test results
    parts.append("## Test Results")
    parts.append(test_output if test_output else "No tests were run")
    parts.append("")

    # Lint results
    parts.append("## Lint Results")
    parts.append(lint_output if lint_output else "All checks passed")
    parts.append("")

    # Architecture rules (extract from FORGE.md)
    arch_section = _extract_architecture_section(session.forge_md_content)
    if arch_section:
        parts.append("## Architecture Rules (from FORGE.md)")
        parts.append(arch_section)

    return "\n".join(parts)


def _get_git_diff_sync(project_root: str) -> str:
    """同步获取 git diff（Evaluator 在异步上下文调用，但 diff 本身很快）。"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "diff"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception as e:
        logger.debug("git diff failed: %s", e)
        return ""


def _extract_architecture_section(forge_md: str) -> str:
    """从 FORGE.md 中提取 architecture 相关内容。"""
    if not forge_md:
        return ""
    # 找 yaml 块中的 architecture 部分
    yaml_blocks = re.findall(r"```yaml\s*\n(.*?)```", forge_md, re.DOTALL)
    for block in yaml_blocks:
        if "architecture:" in block or "layers:" in block:
            return block[:1500]
    return ""
