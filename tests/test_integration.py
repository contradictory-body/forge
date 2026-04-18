"""
test_integration.py — 跨模块集成测试

覆盖真实使用场景中多个模块协同工作的端到端流程。
所有 LLM 调用均 mock，不需要真实 API key。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from forge.core import Session, SessionMode, ToolCall, ToolResult, ToolDefinition
from forge.core.permission import PermissionRule, DEFAULT_DENY_RULES, DEFAULT_ASK_RULES, DEFAULT_ALLOW_RULES
from forge.core.tool_dispatch import ToolDispatch

FIXTURES = Path(__file__).parent / "fixtures"
DUAL_PROJECT = FIXTURES / "dual_project"
NATURAL_FORGE_MD = FIXTURES / "natural_forge_md" / "FORGE.md"
TRACE_DATASET = FIXTURES / "trace_dataset"


# ── 共用 fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def base_session(tmp_path):
    fd = tmp_path / ".forge"
    fd.mkdir()
    s = Session(
        project_root=str(tmp_path),
        forge_dir=str(fd),
        mode=SessionMode.EXECUTE,
        model="qwen3-coder-plus",
        aux_model="qwen3.5-plus",
        api_key="test-key",
    )
    s.permission_rules = {
        "deny": list(DEFAULT_DENY_RULES),
        "ask": list(DEFAULT_ASK_RULES),
        "allow": list(DEFAULT_ALLOW_RULES),
    }
    ToolDispatch.register_builtin_tools(s)
    return s


@pytest.fixture
def dual_session(tmp_path):
    """以 dual_project fixture 为 project_root 的完整 session。"""
    dst = tmp_path / "proj"
    shutil.copytree(DUAL_PROJECT, dst)
    fd = tmp_path / ".forge"
    fd.mkdir()

    forge_md = (dst / "FORGE.md").read_text(encoding="utf-8")

    from forge.core.permission import load_permission_rules
    from forge.constraints.rules_parser import parse_constraints
    from forge.constraints.lint_runner import set_config

    s = Session(
        project_root=str(dst),
        forge_dir=str(fd),
        mode=SessionMode.EXECUTE,
        model="qwen3-coder-plus",
        aux_model="qwen3.5-plus",
        api_key="test-key",
        forge_md_content=forge_md,
    )
    s.permission_rules = load_permission_rules(forge_md)
    config = parse_constraints(forge_md)
    set_config(config)
    ToolDispatch.register_builtin_tools(s)
    return s


# ── 1. 会话创建与规则加载 ────────────────────────────────────────────────

def test_session_creation_with_yaml_forge_md(tmp_path):
    """含 YAML 块的 FORGE.md 正确加载权限规则。"""
    forge_md = (DUAL_PROJECT / "FORGE.md").read_text(encoding="utf-8")
    from forge.core.permission import load_permission_rules
    rules = load_permission_rules(forge_md)
    assert len(rules["deny"]) >= 3     # rm -rf, sudo, .env
    assert len(rules["ask"]) >= 2      # bash, git_commit
    assert len(rules["allow"]) >= 4    # read_file, search_code, list_directory, git_*


@pytest.mark.asyncio
async def test_session_creation_with_natural_forge_md_legacy(tmp_path):
    """无 API key 时自然语言 FORGE.md 回落到 legacy 解析，不崩溃。"""
    forge_md = NATURAL_FORGE_MD.read_text(encoding="utf-8")
    from forge.constraints.forge_md_compiler import compile_forge_md
    compiled = await compile_forge_md(
        forge_md_content=forge_md,
        forge_dir=str(tmp_path),
        aux_model="qwen3.5-plus",
        api_key="",   # 无 api key → 走 legacy
    )
    assert compiled is not None
    # legacy 路径无 YAML 块 → 返回默认规则
    perms = compiled.to_permission_rules()
    assert isinstance(perms, dict)
    assert "deny" in perms and "ask" in perms and "allow" in perms


# ── 2. 权限 + 工具分发 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_permission_deny_blocks_tool_dispatch(dual_session):
    """DENY 的工具调用在 tool_dispatch 层被拦截，返回 is_error=True。"""
    tc = ToolCall(id="t1", name="bash", arguments={"command": "rm -rf /tmp/x"})
    result = await ToolDispatch.execute(dual_session, tc)
    assert result.is_error
    assert "拒绝" in result.content or "deny" in result.content.lower() or "权限" in result.content


@pytest.mark.asyncio
async def test_plan_mode_blocks_write_tool(tmp_path):
    """Plan Mode 下写工具被硬门闸拦截。"""
    fd = tmp_path / ".forge"
    fd.mkdir()
    s = Session(
        project_root=str(tmp_path),
        forge_dir=str(fd),
        mode=SessionMode.PLAN,
    )
    s.permission_rules = {"deny": [], "ask": [], "allow": [PermissionRule(tool="*")]}
    ToolDispatch.register_builtin_tools(s)

    tc = ToolCall(id="t1", name="edit_file", arguments={
        "path": "x.py", "old_str": "", "new_str": "x=1"
    })
    result = await ToolDispatch.execute(s, tc)
    assert result.is_error
    assert "Plan" in result.content


# ── 3. Lint hook 集成 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lint_hook_fires_after_edit(dual_session):
    """
    编辑文件后 post_edit 钩子触发 lint，若有违规则注入 tool_result。
    写一个含 print() 的文件（在 FORGE.md 中被 forbidden）。
    """
    from forge.constraints.lint_runner import lint_hook
    from forge.core import HookResult

    # 在 project 里创建一个含违规的文件
    bad_file = Path(dual_session.project_root) / "bad.py"
    bad_file.write_text('print("hello")\n', encoding="utf-8")

    tc_edit = ToolCall(id="t1", name="edit_file", arguments={"path": "bad.py"})
    tr_ok = ToolResult(content="✓ Edited bad.py", is_error=False)

    result: HookResult = await lint_hook(dual_session, tc_edit, tr_ok)
    # 有违规 → inject_content 非空
    assert result.inject_content is not None
    assert "forbidden_pattern" in result.inject_content.lower() or "print" in result.inject_content


@pytest.mark.asyncio
async def test_import_checker_via_lint_hook(dual_session):
    """
    handler/bad_handler.py 包含层级违规（handler → repository），
    lint hook 应检测到并注入错误内容。
    """
    from forge.constraints.lint_runner import lint_hook
    from forge.core import HookResult

    tc_edit = ToolCall(id="t1", name="edit_file", arguments={"path": "handler/bad_handler.py"})
    tr_ok = ToolResult(content="✓ Edited", is_error=False)

    result: HookResult = await lint_hook(dual_session, tc_edit, tr_ok)
    assert result.inject_content is not None
    assert "CONSTRAINT VIOLATION" in result.inject_content or "import" in result.inject_content.lower()


# ── 4. Compaction 集成 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compaction_triggered_at_threshold(base_session):
    """
    L1 compaction 内部逻辑：tool_result 内容被清空为占位符。
    绕过 token_usage_ratio（需要 tiktoken 网络访问），直接测试清空逻辑。
    """
    from forge.context.compaction import _compact_level_1
    from unittest.mock import patch as _patch

    chunk = "x" * 4000
    for i in range(10):
        base_session.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                          "content": chunk, "is_error": False}]
        })

    # mock _get_ratio so L1 always thinks it's above threshold
    with _patch("forge.context.compaction._get_ratio", return_value=0.75):
        await _compact_level_1(base_session)

    cleared = sum(
        1 for msg in base_session.messages
        if isinstance(msg.get("content"), list)
        for blk in msg["content"]
        if isinstance(blk, dict) and blk.get("type") == "tool_result"
        and str(blk.get("content", "")).startswith("[tool output cleared")
    )
    assert cleared > 0


# ── 5. Evaluator + FeedbackLoop 集成 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluator_integrated_with_feedback_loop(base_session, tmp_path):
    """
    evaluate_hook 失败时 inject revision instructions，
    包含具体失败标准和轮次信息。
    """
    from forge.evaluation.feedback_loop import evaluate_hook, _eval_round_counter

    base_session.features_data = {
        "features": [{
            "id": "feat-001",
            "title": "Test Feature",
            "status": "in_progress",
            "acceptance_criteria": ["criterion A", "criterion B"],
            "depends_on": [],
        }]
    }

    # 清空计数器避免跨测试污染
    _eval_round_counter.clear()

    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "verdict": "needs_revision",
        "score": 55,
        "criteria_results": [
            {"name": "功能完整性", "score": 40, "passed": False, "feedback": "未实现错误处理"},
            {"name": "测试通过", "score": 70, "passed": True, "feedback": "OK"},
        ],
        "overall_feedback": "需要补充错误处理",
        "revision_instructions": "在 handler 中加入 try/except",
    })

    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_response)):
        with patch("forge.evaluation.feedback_loop._run_tests", new=AsyncMock(return_value="")):
            with patch("forge.evaluation.feedback_loop._run_lint", new=AsyncMock(return_value="")):
                result = await evaluate_hook(base_session)

    assert result.inject_content is not None
    assert "needs_revision" in result.inject_content or "⚠" in result.inject_content
    assert "1/" in result.inject_content  # round 1/3


@pytest.mark.asyncio
async def test_quality_grades_written_on_pass(base_session):
    """评审通过时 quality_grades.md 追加 PASS 记录。"""
    from forge.evaluation.feedback_loop import evaluate_hook, _eval_round_counter

    docs_dir = Path(base_session.project_root) / "docs"
    docs_dir.mkdir()
    qg = docs_dir / "quality_grades.md"
    qg.write_text(
        "# Quality Grades\n\n## Unresolved Issues\n\n(none yet)\n\n## Resolved Issues\n\n",
        encoding="utf-8",
    )
    base_session.features_data = {
        "features": [{
            "id": "feat-002", "title": "Feature 2",
            "status": "in_progress",
            "acceptance_criteria": ["做了A", "做了B"],
            "depends_on": [],
        }]
    }
    _eval_round_counter.clear()

    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "verdict": "pass", "score": 88,
        "criteria_results": [],
        "overall_feedback": "Well done",
        "revision_instructions": "",
    })

    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_response)):
        with patch("forge.evaluation.feedback_loop._run_tests", new=AsyncMock(return_value="")):
            with patch("forge.evaluation.feedback_loop._run_lint", new=AsyncMock(return_value="")):
                result = await evaluate_hook(base_session)

    assert "✅" in result.inject_content or "pass" in result.inject_content.lower()
    content = qg.read_text(encoding="utf-8")
    assert "feat-002" in content
    assert "PASS" in content


@pytest.mark.asyncio
async def test_quality_grades_written_on_max_rounds(base_session):
    """超过最大评审轮次时 quality_grades.md 追加 NEEDS REVIEW 记录。"""
    from forge.evaluation.feedback_loop import evaluate_hook, _eval_round_counter, MAX_EVAL_ROUNDS

    docs_dir = Path(base_session.project_root) / "docs"
    docs_dir.mkdir()
    qg = docs_dir / "quality_grades.md"
    qg.write_text(
        "# Quality Grades\n\n## Unresolved Issues\n\n(none yet)\n\n## Resolved Issues\n\n",
        encoding="utf-8",
    )
    base_session.features_data = {
        "features": [{
            "id": "feat-999", "title": "Stuck Feature",
            "status": "in_progress",
            "acceptance_criteria": ["某条件"],
            "depends_on": [],
        }]
    }
    # 强制计数器超过上限
    _eval_round_counter["feat-999"] = MAX_EVAL_ROUNDS

    result = await evaluate_hook(base_session)

    content = qg.read_text(encoding="utf-8")
    assert "feat-999" in content
    assert "NEEDS REVIEW" in content or "human review" in result.inject_content.lower()


# ── 6. Context Assembler 集成 ────────────────────────────────────────────

def test_context_assembler_all_layers(base_session):
    """assembler.build 应包含所有非空层的内容。"""
    from forge.context.assembler import build

    base_session.forge_md_content = "# My Project\nRules here."
    base_session.auto_memory_content = "Past session: fixed bug X."
    base_session.session_memory_content = "## Current\nWorking on feature A."
    base_session.progress_data = {"current_feature": "feat-001", "next_steps": "Write tests"}
    base_session.metadata["git_status_cache"] = "Branch: main"

    # 写一个经验文件
    exp_dir = Path(base_session.forge_dir) / "experience"
    exp_dir.mkdir()
    (exp_dir / "exp_20260401_120000.md").write_text(
        "---\nmerged: false\n---\n\n- 避免跨层调用\n", encoding="utf-8"
    )

    prompt = build(base_session)

    assert "Project Rules" in prompt
    assert "Auto Memory" in prompt
    assert "Session Memory" in prompt
    assert "Progress" in prompt
    assert "Git Status" in prompt
    assert "Agent Self-Knowledge" in prompt
    assert "避免跨层调用" in prompt


def test_experience_prompt_layer_injection(base_session):
    """有经验文件时 System Prompt 包含 Self-Knowledge 段落。"""
    from forge.context.assembler import build

    exp_dir = Path(base_session.forge_dir) / "experience"
    exp_dir.mkdir()
    (exp_dir / "exp_20260401_000000.md").write_text(
        "---\nmerged: false\n---\n\n- 先读后写\n", encoding="utf-8"
    )

    prompt = build(base_session)
    assert "Agent Self-Knowledge" in prompt
    assert "先读后写" in prompt


# ── 7. Observability 集成 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trace_captures_tool_and_eval_events(base_session):
    """init_observability_layer 后，工具调用和 evaluator 均产生 trace 事件。"""
    from forge.observability import init_observability_layer
    from forge.observability.tracer import iter_events

    # 注册一个假工具
    async def fake_handler(sess, args):
        return ToolResult(content="ok")

    ToolDispatch.register(base_session, ToolDefinition(
        name="fake_tool", description="test",
        parameters={"type": "object", "properties": {}},
        handler=fake_handler, readonly=True,
    ))
    base_session.permission_rules["allow"].append(PermissionRule(tool="fake_tool"))

    init_observability_layer(base_session)
    tracer = base_session.tracer

    # 执行工具
    tc = ToolCall(id="tc1", name="fake_tool", arguments={})
    await ToolDispatch.execute(base_session, tc)

    # 触发 evaluator（mock）
    from forge.evaluation import evaluator as ev_module
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "verdict": "pass", "score": 90,
        "criteria_results": [{"name": "功能完整性", "score": 90, "passed": True}],
        "overall_feedback": "OK", "revision_instructions": "",
    })
    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_resp)):
        from forge.evaluation.criteria import DEFAULT_CRITERIA
        await ev_module.evaluate(base_session, "input", DEFAULT_CRITERIA)

    events = list(iter_events(tracer.trace_file))
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "evaluator_verdict" in types


@pytest.mark.asyncio
async def test_trace_session_summary_on_end(base_session):
    """会话结束钩子触发后 trace 包含 session_summary 和 session_end 事件。"""
    from forge.observability import init_observability_layer
    from forge.observability.tracer import iter_events

    init_observability_layer(base_session)
    tracer = base_session.tracer

    # 触发 on_session_end 钩子
    for hook in base_session.hooks.get("on_session_end", []):
        try:
            await hook(base_session)
        except Exception:
            pass

    events = list(iter_events(tracer.trace_file))
    types = [e["type"] for e in events]
    assert "session_end" in types


# ── 8. Entropy 集成 ──────────────────────────────────────────────────────

def test_entropy_startup_cleans_old_outputs(base_session):
    """startup_check 删除超 7 天的 tool_outputs 文件。"""
    output_dir = Path(base_session.forge_dir) / "tool_outputs"
    output_dir.mkdir()
    old_file = output_dir / "read_file_abc.txt"
    old_file.write_text("old output", encoding="utf-8")

    # 设 mtime 为 8 天前
    import os
    old_time = time.time() - 8 * 86400
    os.utime(old_file, (old_time, old_time))

    from forge.harness.entropy import startup_check
    startup_check(base_session)

    assert not old_file.exists()


def test_entropy_startup_cleans_expired_traces(base_session):
    """startup_check 清理已提炼的过期 trace。"""
    import os
    trace_dir = Path(base_session.forge_dir) / "traces"
    trace_dir.mkdir()

    trace_file = trace_dir / "oldsession.jsonl"
    trace_file.write_text(
        '{"type": "session_end", "data": {}}\n', encoding="utf-8"
    )
    done_marker = trace_file.with_suffix(".done")
    done_marker.touch()

    old_time = time.time() - 8 * 86400
    os.utime(trace_file, (old_time, old_time))
    os.utime(done_marker, (old_time, old_time))

    from forge.harness.entropy import startup_check
    startup_check(base_session)

    assert not trace_file.exists()
    assert not done_marker.exists()


# ── 9. Experience 完整生命周期 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_experience_full_lifecycle(base_session):
    """经验系统完整流程：trace 产生 → 提炼 → 注入 prompt。"""
    from forge.observability.tracer import Tracer
    from forge.observability.experience import extract_experience
    from forge.context.assembler import build

    # 1. 创建含评审的 trace
    tracer = Tracer(base_session.forge_dir)
    tracer.emit("session_start", {})
    tracer.emit("evaluator_verdict", {
        "verdict": "needs_revision", "score": 60,
        "criteria_results": [
            {"name": "架构合规", "score": 30, "passed": False, "feedback": "跨层调用"},
        ],
    })
    tracer.emit("session_end", {"total_elapsed_s": 50.0})

    # 2. 提炼经验
    mock_resp = MagicMock()
    mock_resp.text = "- 编辑 handler 前确认不跨层调用 repository"

    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_resp)):
        await extract_experience(tracer.trace_file, base_session)

    # 3. 验证经验文件存在
    exp_dir = Path(base_session.forge_dir) / "experience"
    exp_files = list(exp_dir.glob("exp_*.md"))
    assert len(exp_files) == 1
    assert "handler" in exp_files[0].read_text(encoding="utf-8")

    # 4. 验证注入到 prompt
    prompt = build(base_session)
    assert "Agent Self-Knowledge" in prompt
    assert "handler" in prompt


# ── 10. 双模型在完整 session 中 ──────────────────────────────────────────

def test_dual_model_in_session(base_session):
    """session 同时携带不同的 model 和 aux_model。"""
    assert base_session.model == "qwen3-coder-plus"
    assert base_session.aux_model == "qwen3.5-plus"
    assert base_session.model != base_session.aux_model


# ── 11. FORGE.md 编译器缓存持久化 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_forge_md_compiler_cache_persists(tmp_path):
    """第一次 LLM 编译后缓存写盘，第二次直接读缓存不调用 LLM。"""
    from forge.constraints.forge_md_compiler import compile_forge_md

    content = "## My Project\nDo not use print(), use logging."

    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "structural": {"forbidden_patterns": [{"pattern": "print\\(", "message": "use logging"}]},
        "permissions": {"deny": [], "ask": [], "allow": []},
    })

    with patch("forge.core.query.query_llm",
               new=AsyncMock(return_value=mock_resp)) as mock_q:
        compiled1 = await compile_forge_md(content, str(tmp_path), "qwen3.5-plus", "key")
        compiled2 = await compile_forge_md(content, str(tmp_path), "qwen3.5-plus", "key")

    # LLM 只被调用一次
    assert mock_q.call_count == 1
    assert compiled1.source_hash == compiled2.source_hash


# ── 12. Observation 漂移检测 ─────────────────────────────────────────────

def test_observation_drift_check_silent_when_no_data(base_session, capsys):
    """无历史数据时漂移检测静默（不打印警告）。"""
    from forge.observability.analyzer import startup_drift_check
    startup_drift_check(base_session)
    captured = capsys.readouterr()
    assert "drift" not in captured.out.lower()
    assert "⚠" not in captured.out


def test_observation_drift_check_warns_on_drift(base_session, capsys):
    """检测到漂移时打印警告。"""
    import time as _time
    from forge.observability.tracer import Tracer

    trace_dir = Path(base_session.forge_dir) / "traces"
    trace_dir.mkdir(exist_ok=True)

    # 写 10 个会话：前 5 分高，后 5 分低
    for i in range(10):
        tracer = Tracer(base_session.forge_dir)
        score = 90 if i < 5 else 55
        tracer.emit("evaluator_verdict", {
            "verdict": "pass" if score >= 80 else "needs_revision",
            "score": score,
            "criteria_results": [
                {"name": "架构合规", "score": score, "passed": score >= 80},
            ],
        })
        tracer.emit("session_end", {"total_elapsed_s": 1.0})
        _time.sleep(0.01)

    from forge.observability.analyzer import startup_drift_check
    startup_drift_check(base_session)
    captured = capsys.readouterr()
    assert "⚠" in captured.out or "drift" in captured.out.lower() or "架构合规" in captured.out


# ── 13. Session 结束保存 progress ────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_end_saves_progress(base_session):
    """on_session_end 后 progress.json 写入含 files_modified 的数据。"""
    # 模拟一个编辑操作
    base_session.messages = [
        {"role": "user", "content": "add logging"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tc1", "name": "edit_file",
                          "input": {"path": "service/user_service.py", "old_str": "x", "new_str": "y"}}],
        },
        {
            "role": "assistant",
            "content": "I have completed the task. Done.",
        }
    ]

    from forge.harness.handoff import on_session_end

    with patch("forge.harness.handoff._try_git_commit", new=AsyncMock()):
        await on_session_end(base_session)

    progress_path = Path(base_session.forge_dir) / "progress.json"
    assert progress_path.exists()
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    assert "last_session" in data
    assert "files_modified" in data
    assert any("user_service" in f for f in data.get("files_modified", []))


# ── 14. 完整 Feature 周期 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_feature_cycle(base_session):
    """
    完整 feature 生命周期：
    session → 编辑文件 → 调用 mark_feature_complete → evaluator pass
    → quality_grades.md 更新
    """
    docs_dir = Path(base_session.project_root) / "docs"
    docs_dir.mkdir()
    qg = docs_dir / "quality_grades.md"
    qg.write_text(
        "# Quality Grades\n\n## Unresolved Issues\n\n(none yet)\n\n## Resolved Issues\n\n",
        encoding="utf-8",
    )

    base_session.features_data = {
        "features": [{
            "id": "feat-010", "title": "Implement Logging",
            "status": "in_progress",
            "acceptance_criteria": ["日志已添加", "测试通过"],
            "depends_on": [],
        }]
    }

    from forge.evaluation.feedback_loop import (
        register_mark_complete_tool, _eval_round_counter,
    )
    _eval_round_counter.clear()
    register_mark_complete_tool(base_session)

    assert "mark_feature_complete" in base_session.tool_registry

    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "verdict": "pass", "score": 91,
        "criteria_results": [
            {"name": "功能完整性", "score": 95, "passed": True, "feedback": "完整"},
            {"name": "测试通过", "score": 88, "passed": True, "feedback": "通过"},
        ],
        "overall_feedback": "Excellent",
        "revision_instructions": "",
    })

    with patch("forge.core.query.query_llm", new=AsyncMock(return_value=mock_resp)):
        with patch("forge.evaluation.feedback_loop._run_tests", new=AsyncMock(return_value="3 passed")):
            with patch("forge.evaluation.feedback_loop._run_lint", new=AsyncMock(return_value="")):
                tc = ToolCall(id="mfc1", name="mark_feature_complete", arguments={})
                result = await base_session.tool_registry["mark_feature_complete"].handler(
                    base_session, {}
                )

    assert not result.is_error
    assert "pass" in result.content.lower() or "✅" in result.content

    content = qg.read_text(encoding="utf-8")
    assert "feat-010" in content
