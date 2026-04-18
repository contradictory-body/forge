"""
feedback_loop.py — Generator-Evaluator 迭代循环

核心设计：迭代是自然发生的。
evaluate_hook 返回 inject_content → loop.py 追加到 messages →
agent 看到修改指令 → 修复 → 再次调用 mark_feature_complete →
再次触发 evaluate_hook → 自然形成迭代循环。

mark_feature_complete 工具注册后，agent 调用它来声明 feature 完成。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .evaluator import evaluate
from .diff_formatter import format_eval_input
from .criteria import DEFAULT_CRITERIA

if TYPE_CHECKING:
    from ..core import Session, HookResult

logger = logging.getLogger("forge.evaluation.feedback_loop")

MAX_EVAL_ROUNDS = 3
PASS_THRESHOLD = 80

# 模块级轮次计数器（按 feature id 追踪）
_eval_round_counter: dict[str, int] = {}


def register_mark_complete_tool(session: Session) -> None:
    """注册 mark_feature_complete 工具到 session.tool_registry。"""
    from ..core import ToolDefinition, ToolResult
    from ..core.tool_dispatch import ToolDispatch

    async def _handler(sess: Session, arguments: dict) -> ToolResult:
        """mark_feature_complete 工具 handler。内部触发 evaluate_hook。"""
        hook_result = await evaluate_hook(sess)
        content = hook_result.inject_content or "Feature evaluation skipped."
        return ToolResult(content=content, is_error=False)

    tool_def = ToolDefinition(
        name="mark_feature_complete",
        description=(
            "Call this when you have finished implementing the current feature. "
            "This triggers an independent code review by the Evaluator. "
            "If the review passes, the feature is marked as done. "
            "If it fails, you will receive specific revision instructions."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_handler,
        readonly=False,
    )
    ToolDispatch.register(session, tool_def)
    logger.debug("Registered mark_feature_complete tool")


async def evaluate_hook(session: Session) -> HookResult:
    """
    on_feature_complete 钩子 / mark_feature_complete 工具内部调用。

    1. 找到当前 feature
    2. 收集自动信号（test / lint）
    3. 调用 Evaluator（独立上下文）
    4. 根据结果返回 pass / needs_revision / exceeded_max_rounds
    """
    from ..core import HookResult

    # 1. 确定当前 feature
    feature = _get_current_feature(session)
    if feature is None:
        return HookResult(
            inject_content="No feature currently in progress. Nothing to evaluate."
        )

    feature_id = feature.get("id", "unknown")

    # 检查轮次
    current_round = _eval_round_counter.get(feature_id, 0) + 1
    _eval_round_counter[feature_id] = current_round

    if current_round > MAX_EVAL_ROUNDS:
        # 超过最大轮次
        _record_quality_issue(session, feature, "max rounds exceeded")
        return HookResult(
            inject_content=(
                f"Feature {feature_id} did not pass review after {MAX_EVAL_ROUNDS} rounds.\n"
                f"Marking for human review.\n"
                f"Recorded in docs/quality_grades.md."
            )
        )

    # 2. 收集自动信号
    test_output = await _run_tests(session)
    lint_output = await _run_lint(session)

    # 3. 格式化评审输入
    eval_input = format_eval_input(session, feature, test_output, lint_output)

    # 4. 执行评审（独立上下文）
    result = await evaluate(session, eval_input, DEFAULT_CRITERIA)

    verdict = result.get("verdict", "pass")
    score = result.get("score", 80)
    overall_feedback = result.get("overall_feedback", "")
    revision_instructions = result.get("revision_instructions", "")

    # 5. pass
    if verdict == "pass" or score >= PASS_THRESHOLD:
        _eval_round_counter.pop(feature_id, None)  # 重置计数器
        _record_quality_pass(session, feature, score, overall_feedback)

        # ── 更新 features.json ──────────────────────────────────────────
        # 这是 mark_feature_complete 的核心职责：推进 feature 状态
        try:
            from ..context.progress import mark_feature_done, get_next_feature, save_features
            # commit_sha: 尝试获取当前 git HEAD，不可用时用空字符串
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=session.project_root,
                    capture_output=True, text=True, timeout=5,
                )
                commit_sha = result.stdout.strip() if result.returncode == 0 else ""
            except Exception:
                commit_sha = ""
            mark_feature_done(session.features_data, feature_id, commit_sha)
            next_f = get_next_feature(session.features_data)
            if next_f and next_f.get("status") != "done":
                next_f["status"] = "in_progress"
            save_features(session.forge_dir, session.features_data)
            logger.info(
                "Feature %s marked done (commit=%s). Next: %s",
                feature_id, commit_sha,
                next_f.get("id") if next_f else "none",
            )
        except Exception as e:
            logger.warning("Failed to update features.json: %s", e)
        # ────────────────────────────────────────────────────────────────

        return HookResult(
            inject_content=(
                f"✅ Feature {feature_id} passed review "
                f"(score: {score}/100, round {current_round}).\n"
                f"Feedback: {overall_feedback}"
            )
        )

    # 6. needs_revision
    criteria_results = result.get("criteria_results", [])
    failed_items = [
        c for c in criteria_results
        if isinstance(c, dict) and not c.get("passed", True)
    ]
    failed_text = "\n".join(
        f"  - {c.get('name', '?')}: {c.get('feedback', '(no feedback)')}"
        for c in failed_items
    )

    return HookResult(
        inject_content=(
            f"⚠️ Feature {feature_id} needs revision "
            f"(score: {score}/100, round {current_round}/{MAX_EVAL_ROUNDS}).\n"
            f"Issues:\n{failed_text}\n"
            f"Instructions: {revision_instructions}\n"
            f"Please fix the issues above and call mark_feature_complete again when done."
        )
    )


# ── 辅助函数 ─────────────────────────────────────────────────────────────

def _get_current_feature(session: Session) -> dict | None:
    """从 features_data 中找 in_progress 或第一个 pending 的 feature。"""
    if not session.features_data:
        return None
    features = session.features_data.get("features", [])
    for f in features:
        if f.get("status") == "in_progress":
            return f
    done_ids = {f["id"] for f in features if f.get("status") == "done"}
    for f in features:
        if f.get("status") == "pending":
            deps = f.get("depends_on", [])
            if all(d in done_ids for d in deps):
                return f
    return None


async def _run_tests(session: Session) -> str:
    """尝试运行项目测试。"""
    # 尝试常见测试命令
    test_commands = ["pytest --tb=short -q", "npm test", "make test"]
    for cmd in test_commands:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=session.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode is not None:  # 命令存在并执行了
                err = stderr.decode("utf-8", errors="replace")
                return f"Command: {cmd}\nExit code: {proc.returncode}\n{output}\n{err}".strip()[:3000]
        except (asyncio.TimeoutError, FileNotFoundError):
            continue
        except Exception:
            continue
    return ""


async def _run_lint(session: Session) -> str:
    """尝试运行 lint。"""
    from ..constraints.lint_runner import _constraint_config
    if _constraint_config and _constraint_config.lint.command:
        cmd = _constraint_config.lint.command
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=session.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                return f"Lint failed:\n{output}\n{err}".strip()[:2000]
            return ""
        except Exception:
            pass
    return ""


def _record_quality_pass(
    session: Session, feature: dict, score: int, feedback: str
) -> None:
    """在 quality_grades.md 中记录通过信息。"""
    qg_path = Path(session.project_root) / "docs" / "quality_grades.md"
    try:
        if qg_path.exists():
            content = qg_path.read_text(encoding="utf-8")
        else:
            qg_path.parent.mkdir(parents=True, exist_ok=True)
            content = "# Quality Grades\n\n## Unresolved Issues\n\n(none yet)\n\n## Resolved Issues\n\n"

        entry = (
            f"- [{datetime.now().strftime('%Y-%m-%d %H:%M')}] "
            f"{feature.get('id')}: PASS (score={score}) — {feedback[:100]}\n"
        )
        content = content.replace("## Resolved Issues\n\n", f"## Resolved Issues\n\n{entry}")
        qg_path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to record quality pass: %s", e)


def _record_quality_issue(
    session: Session, feature: dict, reason: str
) -> None:
    """在 quality_grades.md 中记录未解决问题。"""
    qg_path = Path(session.project_root) / "docs" / "quality_grades.md"
    try:
        if qg_path.exists():
            content = qg_path.read_text(encoding="utf-8")
        else:
            qg_path.parent.mkdir(parents=True, exist_ok=True)
            content = "# Quality Grades\n\n## Unresolved Issues\n\n(none yet)\n\n## Resolved Issues\n\n"

        entry = (
            f"- [{datetime.now().strftime('%Y-%m-%d %H:%M')}] "
            f"{feature.get('id')}: NEEDS REVIEW — {reason}\n"
        )
        # 去掉 (none yet)
        content = content.replace("(none yet)\n", "")
        content = content.replace("## Unresolved Issues\n\n", f"## Unresolved Issues\n\n{entry}")
        qg_path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to record quality issue: %s", e)
