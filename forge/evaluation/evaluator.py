"""
evaluator.py — Evaluator Agent（独立上下文）

核心设计：Evaluator 运行在完全独立的 LLM 调用中，
不共享 Generator 的对话历史。它只看到代码 diff 和测试结果。
评审失败时 fail-open（不阻塞 agent 工作）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session
    from .criteria import EvalCriterion

logger = logging.getLogger("forge.evaluation.evaluator")

_PASS_THRESHOLD = 80

_SYSTEM_PROMPT_TEMPLATE = """你是一个严格的代码审查员。你的任务是评审一个 coding agent 的工作产出。
你只看到代码变更和测试结果，你不知道 agent 的思考过程。
请根据以下评审标准逐项评分。

评审标准：
{criteria_text}

重要规则：
- 如果 auto_signal 为 test_result 且测试失败，该项得分不得超过权重的 30%
- 如果 auto_signal 为 lint_result 且存在违规，该项得分不得超过权重的 30%
- 总分 >= {threshold} → verdict 为 "pass"
- 总分 < {threshold} → verdict 为 "needs_revision"

输出格式：严格 JSON（不要输出其他任何文字），schema：
{{
    "verdict": "pass" | "needs_revision",
    "score": 0-100,
    "criteria_results": [
        {{
            "name": "标准名称",
            "score": 分数,
            "passed": true/false,
            "feedback": "具体反馈"
        }}
    ],
    "overall_feedback": "总体评价",
    "revision_instructions": "具体修改建议（仅在 needs_revision 时填写）"
}}"""


def _build_criteria_text(criteria: list[EvalCriterion]) -> str:
    """将评审标准格式化为文本。"""
    lines: list[str] = []
    for c in criteria:
        signal_note = f" (auto_signal: {c.auto_signal})" if c.auto_signal != "none" else ""
        lines.append(f"- {c.name} (weight: {c.weight}%){signal_note}: {c.description}")
    return "\n".join(lines)


async def evaluate(
    session: Session,
    eval_input: str,
    criteria: list[EvalCriterion],
) -> dict:
    """
    执行独立评审。

    在独立上下文中调用 LLM，不共享 Generator 的对话历史。
    解析失败时 fail-open（返回默认 pass）。
    """
    criteria_text = _build_criteria_text(criteria)
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        criteria_text=criteria_text,
        threshold=_PASS_THRESHOLD,
    )

    try:
        from ..core.query import query_llm

        response = await query_llm(
            model=session.aux_model,
            api_key=session.api_key,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": eval_input}],
            tool_definitions=[],
        )
        raw = response.text
    except Exception as e:
        logger.warning("Evaluator LLM call failed: %s, fail-open (default pass)", e)
        return _default_pass()

    # 解析 JSON
    result = _parse_eval_response(raw)
    if result is None:
        logger.warning("Failed to parse evaluator JSON, fail-open (default pass)")
        return _default_pass()

    logger.info(
        "Evaluation complete: verdict=%s, score=%s",
        result.get("verdict"), result.get("score"),
    )
    return result


def _parse_eval_response(raw: str) -> dict | None:
    """解析 Evaluator 的 JSON 响应，容错处理。"""
    text = raw.strip()

    # 去除 markdown 代码块包裹
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试找 { ... }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None

    # 基本结构验证
    if not isinstance(data, dict):
        return None
    if "verdict" not in data or "score" not in data:
        return None

    return data


def _default_pass() -> dict:
    """fail-open 默认通过。"""
    return {
        "verdict": "pass",
        "score": 80,
        "criteria_results": [],
        "overall_feedback": "Evaluation skipped (LLM unavailable or parse error). Default pass.",
        "revision_instructions": "",
    }
