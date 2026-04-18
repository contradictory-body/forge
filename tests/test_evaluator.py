"""
test_evaluator.py — Generator-Evaluator 评审测试

覆盖场景：
1. 评审通过：score=85 → HookResult 包含 ✅
2. 评审不通过：score=60 → inject_content 包含修改建议
3. JSON 解析失败 → fail-open（默认通过）
4. MAX_EVAL_ROUNDS 上限 → 记录到 quality_grades.md
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from forge.core import Session, HookResult
from forge.evaluation.evaluator import evaluate, _parse_eval_response, _default_pass
from forge.evaluation.criteria import DEFAULT_CRITERIA


@pytest.fixture
def session(tmp_path):
    fd = tmp_path / ".forge"; fd.mkdir()
    (tmp_path / "docs").mkdir()
    return Session(
        project_root=str(tmp_path),
        forge_dir=str(fd),
        features_data={
            "version": 1,
            "features": [
                {
                    "id": "feat-001",
                    "title": "User Auth",
                    "status": "in_progress",
                    "acceptance_criteria": ["JWT signing", "Password hashing"],
                    "depends_on": [],
                },
            ],
        },
    )


def _make_eval_response(verdict: str, score: int) -> str:
    return json.dumps({
        "verdict": verdict,
        "score": score,
        "criteria_results": [
            {"name": "功能完整性", "score": 20, "passed": verdict == "pass",
             "feedback": "Good" if verdict == "pass" else "Missing JWT"},
        ],
        "overall_feedback": "Looks good" if verdict == "pass" else "Needs work",
        "revision_instructions": "" if verdict == "pass" else "Add JWT signing logic",
    })


# ── 1. 评审通过 ─────────────────────────────────────────────────────────

class TestEvaluatorPass:

    @pytest.mark.asyncio
    async def test_pass_score_85(self, session):
        """mock query_llm 返回 score=85, verdict=pass → 通过。"""
        mock_resp = MagicMock()
        mock_resp.text = _make_eval_response("pass", 85)

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            result = await evaluate(session, "eval input text", DEFAULT_CRITERIA)

        assert result["verdict"] == "pass"
        assert result["score"] == 85


# ── 2. 评审不通过 ────────────────────────────────────────────────────────

class TestEvaluatorFail:

    @pytest.mark.asyncio
    async def test_needs_revision_score_60(self, session):
        """score=60, verdict=needs_revision → 不通过。"""
        mock_resp = MagicMock()
        mock_resp.text = _make_eval_response("needs_revision", 60)

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            result = await evaluate(session, "eval input", DEFAULT_CRITERIA)

        assert result["verdict"] == "needs_revision"
        assert result["score"] == 60
        assert result["revision_instructions"] == "Add JWT signing logic"


# ── 3. JSON 解析失败 → fail-open ────────────────────────────────────────

class TestEvaluatorFailOpen:

    @pytest.mark.asyncio
    async def test_invalid_json_returns_default_pass(self, session):
        """LLM 返回非 JSON → fail-open（默认通过）。"""
        mock_resp = MagicMock()
        mock_resp.text = "This is not JSON at all, just random text."

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            result = await evaluate(session, "eval input", DEFAULT_CRITERIA)

        assert result["verdict"] == "pass"
        assert result["score"] == 80  # default pass score

    @pytest.mark.asyncio
    async def test_llm_exception_returns_default_pass(self, session):
        """LLM 调用异常 → fail-open。"""
        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("API down")
            result = await evaluate(session, "eval input", DEFAULT_CRITERIA)

        assert result["verdict"] == "pass"


# ── 4. MAX_EVAL_ROUNDS 上限 ─────────────────────────────────────────────

class TestFeedbackLoop:

    @pytest.mark.asyncio
    async def test_max_rounds_exceeded(self, session):
        """模拟超过 MAX_EVAL_ROUNDS → 记录到 quality_grades.md。"""
        from forge.evaluation.feedback_loop import (
            evaluate_hook, _eval_round_counter, MAX_EVAL_ROUNDS,
        )

        # 清空计数器，避免跨测试污染
        _eval_round_counter.clear()
        # 预设轮次计数器已达上限
        _eval_round_counter["feat-001"] = MAX_EVAL_ROUNDS

        result = await evaluate_hook(session)

        assert result.inject_content is not None
        assert "did not pass review" in result.inject_content
        assert "human review" in result.inject_content

        # 检查 quality_grades.md 是否被写入
        qg_path = Path(session.project_root) / "docs" / "quality_grades.md"
        assert qg_path.exists()
        qg_content = qg_path.read_text(encoding="utf-8")
        assert "feat-001" in qg_content
        assert "NEEDS REVIEW" in qg_content

        # 清理
        _eval_round_counter.clear()

    @pytest.mark.asyncio
    async def test_pass_resets_counter(self, session):
        """评审通过后轮次计数器应被重置。"""
        from forge.evaluation.feedback_loop import evaluate_hook, _eval_round_counter

        # 清空计数器，避免跨测试污染
        _eval_round_counter.clear()

        mock_resp = MagicMock()
        mock_resp.text = _make_eval_response("pass", 90)

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            # Also mock _run_tests and _run_lint to avoid subprocess calls
            with patch("forge.evaluation.feedback_loop._run_tests", new_callable=AsyncMock) as mt:
                mt.return_value = "all passed"
                with patch("forge.evaluation.feedback_loop._run_lint", new_callable=AsyncMock) as ml:
                    ml.return_value = ""
                    result = await evaluate_hook(session)

        assert result.inject_content is not None
        assert "✅" in result.inject_content
        assert "feat-001" not in _eval_round_counter

        # 清理
        _eval_round_counter.clear()

    @pytest.mark.asyncio
    async def test_needs_revision_injects_instructions(self, session):
        """不通过时 inject_content 包含修改建议。"""
        from forge.evaluation.feedback_loop import evaluate_hook, _eval_round_counter
        _eval_round_counter.clear()

        mock_resp = MagicMock()
        mock_resp.text = _make_eval_response("needs_revision", 55)

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            with patch("forge.evaluation.feedback_loop._run_tests", new_callable=AsyncMock) as mt:
                mt.return_value = ""
                with patch("forge.evaluation.feedback_loop._run_lint", new_callable=AsyncMock) as ml:
                    ml.return_value = ""
                    result = await evaluate_hook(session)

        assert result.inject_content is not None
        assert "⚠️" in result.inject_content
        assert "needs revision" in result.inject_content.lower()
        assert "mark_feature_complete" in result.inject_content

        _eval_round_counter.clear()


# ── _parse_eval_response 单元测试 ────────────────────────────────────────

class TestParseEvalResponse:

    def test_valid_json(self):
        raw = _make_eval_response("pass", 85)
        result = _parse_eval_response(raw)
        assert result is not None
        assert result["verdict"] == "pass"

    def test_json_in_markdown_block(self):
        raw = "```json\n" + _make_eval_response("pass", 90) + "\n```"
        result = _parse_eval_response(raw)
        assert result is not None
        assert result["score"] == 90

    def test_invalid_returns_none(self):
        assert _parse_eval_response("not json") is None

    def test_missing_verdict_returns_none(self):
        assert _parse_eval_response('{"score": 80}') is None

    def test_default_pass(self):
        d = _default_pass()
        assert d["verdict"] == "pass"
        assert d["score"] == 80
