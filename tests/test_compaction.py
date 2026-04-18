"""
test_compaction.py — 三级 Compaction 策略测试

覆盖场景：
1. Level 1 触发：清理旧 tool output
2. Level 2 触发：用 session memory 替换旧历史
3. Level 2 文件重注入
4. Level 3 触发：LLM 全量摘要
5. 级联触发：Level 2 → Level 3
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from forge.core import Session, SessionMode


def _make_session(tmp_path, max_tokens=200000) -> Session:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir(exist_ok=True)
    return Session(
        project_root=str(tmp_path),
        forge_dir=str(forge_dir),
        max_tokens=max_tokens,
    )


def _fill_tool_results(session: Session, target_ratio: float) -> None:
    """填充 tool_result 消息使 token_usage_ratio 达到 target_ratio。"""
    chunk_size = 4000
    max_iters = 200
    for _ in range(max_iters):
        if session.token_usage_ratio >= target_ratio:
            break
        session.messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": f"tc_{len(session.messages)}",
                "content": "x" * chunk_size,
                "is_error": False,
            }],
        })


def _fill_large_messages(session: Session, count: int, chars_per_msg: int = 5000) -> None:
    """填充大量大消息（用于测试 Level 2/3 的 keep_count 逻辑）。"""
    for i in range(count):
        session.messages.append({
            "role": "user",
            "content": f"user_msg_{i} " + "U" * chars_per_msg,
        })
        session.messages.append({
            "role": "assistant",
            "content": f"reply_{i} " + "A" * chars_per_msg,
        })


# ── Level 1 Tests ────────────────────────────────────────────────────────

class TestCompactLevel1:

    @pytest.mark.asyncio
    async def test_clears_old_tool_outputs(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=1000)
        session.messages.append({"role": "user", "content": "hello"})
        session.messages.append({"role": "assistant", "content": "hi"})
        _fill_tool_results(session, 0.75)
        assert session.token_usage_ratio > 0.70

        from forge.context.compaction import _compact_level_1
        await _compact_level_1(session)

        cleared = sum(
            1
            for msg in session.messages
            if isinstance(msg.get("content"), list)
            for block in msg["content"]
            if isinstance(block, dict)
            and block.get("type") == "tool_result"
            and isinstance(block.get("content", ""), str)
            and block["content"].startswith("[tool output cleared")
        )
        assert cleared > 0

    @pytest.mark.asyncio
    async def test_preserves_non_tool_messages(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=1000)
        session.messages.append({"role": "user", "content": "important question"})
        session.messages.append({"role": "assistant", "content": "important answer"})
        _fill_tool_results(session, 0.75)

        from forge.context.compaction import _compact_level_1
        await _compact_level_1(session)

        text_msgs = [m for m in session.messages if isinstance(m.get("content"), str)]
        assert len(text_msgs) >= 2


# ── Level 2 Tests ────────────────────────────────────────────────────────

class TestCompactLevel2:

    @pytest.mark.asyncio
    async def test_replaces_old_messages_with_summary(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=200000)
        session.session_memory_content = "This is a session summary."
        # 40 pairs x 5000 chars = 400k chars total, well over KEEP_TOKEN_BUDGET
        _fill_large_messages(session, count=40, chars_per_msg=5000)
        original_count = len(session.messages)

        from forge.context.compaction import _compact_level_2
        await _compact_level_2(session)

        assert len(session.messages) < original_count
        assert "[Previous conversation compacted" in session.messages[0].get("content", "")
        assert "session summary" in session.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_reinjects_recently_read_files(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=200000)
        session.session_memory_content = "summary"

        test_file = tmp_path / "src" / "main.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("print('hello world')\n", encoding="utf-8")
        session.recently_read_files = [str(test_file)]

        _fill_large_messages(session, count=40, chars_per_msg=5000)

        from forge.context.compaction import _compact_level_2
        await _compact_level_2(session)

        reinject_msgs = [
            m for m in session.messages
            if isinstance(m.get("content"), str)
            and "[Re-loaded after compaction]" in m["content"]
        ]
        assert len(reinject_msgs) >= 1
        assert "hello world" in reinject_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_falls_back_when_no_session_memory(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=1000)
        session.session_memory_content = ""
        _fill_tool_results(session, 0.90)

        from forge.context.compaction import _compact_level_2
        await _compact_level_2(session)
        # should not crash


# ── Level 3 Tests ────────────────────────────────────────────────────────

class TestCompactLevel3:

    @pytest.mark.asyncio
    async def test_full_summarization(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=200000)
        _fill_large_messages(session, count=40, chars_per_msg=5000)
        original_count = len(session.messages)

        mock_response = MagicMock()
        mock_response.text = "## Primary Request\nBuild a web app\n## Current State\nIn progress"

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_response
            from forge.context.compaction import _compact_level_3
            await _compact_level_3(session)

        assert len(session.messages) < original_count
        assert "Primary Request" in session.session_memory_content

    @pytest.mark.asyncio
    async def test_fallback_on_llm_failure(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=200000)
        session.messages.append({"role": "user", "content": "build me a website"})
        session.messages.append({"role": "assistant", "content": "ok, starting"})
        _fill_large_messages(session, count=30, chars_per_msg=5000)

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("API down")
            from forge.context.compaction import _compact_level_3
            await _compact_level_3(session)

        assert session.session_memory_content
        assert "build me a website" in session.session_memory_content


# ── Cascade Tests ────────────────────────────────────────────────────────

class TestCompactCascade:

    @pytest.mark.asyncio
    async def test_level2_escalates_to_level3(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=2000)
        session.session_memory_content = "short summary"
        _fill_large_messages(session, count=20, chars_per_msg=5000)

        mock_response = MagicMock()
        mock_response.text = "Cascaded summary from Level 3"

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_response
            from forge.context.compaction import _compact_level_2
            await _compact_level_2(session)

        assert len(session.messages) < 40

    @pytest.mark.asyncio
    async def test_compact_entry_selects_level3(self, tmp_path):
        session = _make_session(tmp_path, max_tokens=2000)
        _fill_large_messages(session, count=20, chars_per_msg=5000)
        assert session.token_usage_ratio > 0.92

        mock_response = MagicMock()
        mock_response.text = "Full summary"

        with patch("forge.core.query.query_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_response
            from forge.context.compaction import compact
            await compact(session)

        assert session.compaction_count == 1


# ── Progress Tests ───────────────────────────────────────────────────────

class TestProgress:

    def test_load_and_save_features(self, tmp_path):
        from forge.context.progress import save_features, load_features
        data = {"version": 1, "features": [
            {"id": "feat-001", "title": "Auth", "status": "pending"},
            {"id": "feat-002", "title": "API", "status": "done"},
        ]}
        save_features(str(tmp_path), data)
        loaded = load_features(str(tmp_path))
        assert loaded is not None
        assert len(loaded["features"]) == 2

    def test_load_nonexistent_returns_none(self, tmp_path):
        from forge.context.progress import load_features
        assert load_features(str(tmp_path)) is None

    def test_mark_feature_done(self):
        from forge.context.progress import mark_feature_done
        data = {"features": [
            {"id": "feat-001", "status": "in_progress"},
            {"id": "feat-002", "status": "pending"},
        ]}
        mark_feature_done(data, "feat-001", "abc123")
        assert data["features"][0]["status"] == "done"
        assert data["features"][0]["commit_sha"] == "abc123"
        assert "completed_at" in data["features"][0]

    def test_get_next_feature_in_progress_first(self):
        from forge.context.progress import get_next_feature
        data = {"features": [
            {"id": "feat-001", "status": "done"},
            {"id": "feat-002", "status": "in_progress"},
            {"id": "feat-003", "status": "pending", "depends_on": []},
        ]}
        assert get_next_feature(data)["id"] == "feat-002"

    def test_get_next_feature_pending_with_deps(self):
        from forge.context.progress import get_next_feature
        data = {"features": [
            {"id": "feat-001", "status": "done"},
            {"id": "feat-002", "status": "pending", "depends_on": ["feat-001"]},
            {"id": "feat-003", "status": "pending", "depends_on": ["feat-002"]},
        ]}
        assert get_next_feature(data)["id"] == "feat-002"

    def test_get_next_feature_all_done(self):
        from forge.context.progress import get_next_feature
        assert get_next_feature({"features": [
            {"id": "f1", "status": "done"}, {"id": "f2", "status": "done"},
        ]}) is None


# ── Entropy Tests ────────────────────────────────────────────────────────

class TestEntropy:

    def test_cleanup_expired_files(self, tmp_path):
        import os, time
        session = _make_session(tmp_path)
        output_dir = Path(session.forge_dir) / "tool_outputs"
        output_dir.mkdir()

        old_file = output_dir / "old.txt"
        old_file.write_text("old")
        os.utime(old_file, (time.time() - 8 * 86400,) * 2)

        new_file = output_dir / "new.txt"
        new_file.write_text("new")

        from forge.harness.entropy import _cleanup_expired_tool_outputs
        _cleanup_expired_tool_outputs(session)

        assert not old_file.exists()
        assert new_file.exists()
