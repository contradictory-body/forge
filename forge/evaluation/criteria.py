"""
criteria.py — 评审标准定义

定义 Evaluator 的评分维度、权重和自动信号来源。
"""

from dataclasses import dataclass


@dataclass
class EvalCriterion:
    """单项评审标准。"""
    name: str
    weight: int          # 权重百分比，所有标准之和 = 100
    description: str     # 给 Evaluator LLM 看的描述
    auto_signal: str     # "test_result" / "lint_result" / "none"


DEFAULT_CRITERIA = [
    EvalCriterion(
        name="功能完整性",
        weight=30,
        description="acceptance_criteria 中列出的所有条件是否都被满足。逐条核对。",
        auto_signal="none",
    ),
    EvalCriterion(
        name="测试通过",
        weight=25,
        description="所有测试是否通过。如果测试结果显示失败，此项不得满分。",
        auto_signal="test_result",
    ),
    EvalCriterion(
        name="架构合规",
        weight=20,
        description=(
            "代码是否遵守 FORGE.md 中定义的架构约束（层级依赖、命名约定等）。"
            "如果 lint 结果显示违规，此项不得满分。"
        ),
        auto_signal="lint_result",
    ),
    EvalCriterion(
        name="代码质量",
        weight=15,
        description="错误处理是否完善，命名是否清晰，函数是否合理拆分，有无明显的代码异味。",
        auto_signal="none",
    ),
    EvalCriterion(
        name="文档",
        weight=10,
        description="关键函数和类是否有 docstring，复杂逻辑是否有注释。",
        auto_signal="none",
    ),
]
