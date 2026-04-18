"""
test_forge_md_compiler.py — 自然语言 FORGE.md 编译器测试

注：_compile_via_llm 里用的是 `from ..core.query import query_llm` 的
deferred import，所以 patch 要打在 source 上：forge.core.query.query_llm。
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from forge.constraints.forge_md_compiler import (
    compile_forge_md, try_load_cache_only, CompiledForgeMD,
    CACHE_FILENAME, CACHE_VERSION, format_compiled_preview,
    _hash, _parse_json_loose,
)


YAML_FORGE_MD = """# Project

```yaml
architecture:
  layers: [types, config, repository, service, handler]
  cross_cutting: [util, common]
```

```yaml
permissions:
  deny:
    - tool: bash
      pattern: "rm\\\\s+-rf"
  ask:
    - tool: git_commit
  allow:
    - tool: read_file
```

```yaml
lint:
  command: "ruff check ."
```
"""


# ── 缓存行为 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_hit_skips_llm(tmp_path):
    forge_dir = str(tmp_path)
    doc = "# My project\nUse ruff to lint."

    cache = {
        "source_hash": _hash(doc),
        "cache_version": CACHE_VERSION,
        "lint": {"command": "ruff check .", "auto_fix_command": ""},
    }
    (tmp_path / CACHE_FILENAME).write_text(json.dumps(cache), encoding="utf-8")

    with patch("forge.core.query.query_llm", new=AsyncMock()) as mock_query:
        compiled = await compile_forge_md(doc, forge_dir, "m", "k")

    mock_query.assert_not_called()
    assert compiled.data["lint"]["command"] == "ruff check ."


@pytest.mark.asyncio
async def test_cache_miss_on_content_change(tmp_path):
    forge_dir = str(tmp_path)
    doc_v1 = "# Project v1"
    doc_v2 = "# Project v2 with more rules"

    cache = {
        "source_hash": _hash(doc_v1),
        "cache_version": CACHE_VERSION,
        "lint": {"command": "old", "auto_fix_command": ""},
    }
    (tmp_path / CACHE_FILENAME).write_text(json.dumps(cache), encoding="utf-8")

    with patch(
        "forge.core.query.query_llm",
        new=AsyncMock(side_effect=RuntimeError("api down")),
    ):
        compiled = await compile_forge_md(doc_v2, forge_dir, "m", "k")

    assert compiled.data.get("lint", {}).get("command") != "old"


@pytest.mark.asyncio
async def test_cache_version_bump_invalidates(tmp_path):
    forge_dir = str(tmp_path)
    doc = "# P"

    cache = {
        "source_hash": _hash(doc),
        "cache_version": CACHE_VERSION - 1,
        "lint": {"command": "stale"},
    }
    (tmp_path / CACHE_FILENAME).write_text(json.dumps(cache), encoding="utf-8")

    with patch(
        "forge.core.query.query_llm",
        new=AsyncMock(side_effect=RuntimeError("offline")),
    ):
        compiled = await compile_forge_md(doc, forge_dir, "m", "k")

    assert compiled.data.get("lint", {}).get("command") != "stale"


def test_try_load_cache_only_hit(tmp_path):
    forge_dir = str(tmp_path)
    doc = "same content"
    cache = {
        "source_hash": _hash(doc),
        "cache_version": CACHE_VERSION,
        "lint": {"command": "x"},
    }
    (tmp_path / CACHE_FILENAME).write_text(json.dumps(cache), encoding="utf-8")

    result = try_load_cache_only(doc, forge_dir)
    assert result is not None
    assert result.data["lint"]["command"] == "x"


def test_try_load_cache_only_miss(tmp_path):
    result = try_load_cache_only("fresh content", str(tmp_path))
    assert result is None


def test_try_load_cache_only_empty_content(tmp_path):
    result = try_load_cache_only("", str(tmp_path))
    assert result is not None
    assert result.to_constraint_config().architecture.layers == []


# ── Legacy fallback ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_fallback_when_llm_fails(tmp_path):
    forge_dir = str(tmp_path)

    with patch(
        "forge.core.query.query_llm",
        new=AsyncMock(side_effect=RuntimeError("network error")),
    ):
        compiled = await compile_forge_md(YAML_FORGE_MD, forge_dir, "m", "k")

    assert compiled.data["_compiled_by"] == "legacy_yaml_parser"

    cfg = compiled.to_constraint_config()
    assert cfg.architecture.layers == ["types", "config", "repository", "service", "handler"]
    assert cfg.lint.command == "ruff check ."

    perms = compiled.to_permission_rules()
    assert len(perms["deny"]) == 1
    assert perms["deny"][0].tool == "bash"
    assert perms["deny"][0].pattern == r"rm\s+-rf"
    assert any(r.tool == "git_commit" for r in perms["ask"])
    assert any(r.tool == "read_file" for r in perms["allow"])


@pytest.mark.asyncio
async def test_no_api_key_uses_legacy(tmp_path):
    forge_dir = str(tmp_path)

    with patch("forge.core.query.query_llm", new=AsyncMock()) as mock_q:
        compiled = await compile_forge_md(YAML_FORGE_MD, forge_dir, "m", api_key="")

    mock_q.assert_not_called()
    assert compiled.data["_compiled_by"] == "legacy_yaml_parser"


# ── LLM 成功路径 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_success_caches_result(tmp_path):
    forge_dir = str(tmp_path)
    doc = "# My project with natural language rules"

    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "architecture": {"layers": ["types", "service", "handler"], "cross_cutting": []},
        "permissions": {
            "deny": [{"tool": "bash", "pattern": "rm\\s+-rf", "path": ""}],
            "ask": [],
            "allow": [{"tool": "read_file", "pattern": "", "path": ""}],
        },
        "lint": {"command": "ruff check .", "auto_fix_command": ""},
        "structural": {"max_file_lines": 400, "max_function_lines": 60},
        "conventions": ["Read before editing", "Run tests"],
    })

    with patch(
        "forge.core.query.query_llm",
        new=AsyncMock(return_value=mock_response),
    ) as mock_q:
        compiled = await compile_forge_md(doc, forge_dir, "m", "k")

    mock_q.assert_called_once()
    assert compiled.source_hash == _hash(doc)
    assert compiled.conventions() == ["Read before editing", "Run tests"]

    cfg = compiled.to_constraint_config()
    assert cfg.architecture.layers == ["types", "service", "handler"]
    assert cfg.structural.max_file_lines == 400

    cache_path = tmp_path / CACHE_FILENAME
    assert cache_path.exists()

    # 再次调用应走缓存（mock 只被调用一次）
    with patch(
        "forge.core.query.query_llm",
        new=AsyncMock(return_value=mock_response),
    ) as mock_q2:
        compiled2 = await compile_forge_md(doc, forge_dir, "m", "k")
    mock_q2.assert_not_called()
    assert compiled2.source_hash == compiled.source_hash


@pytest.mark.asyncio
async def test_llm_returns_garbage_falls_back(tmp_path):
    mock_response = MagicMock()
    mock_response.text = "Here is the JSON you requested, thank you!"

    with patch(
        "forge.core.query.query_llm",
        new=AsyncMock(return_value=mock_response),
    ):
        compiled = await compile_forge_md(YAML_FORGE_MD, str(tmp_path), "m", "k")

    assert compiled.data["_compiled_by"] == "legacy_yaml_parser"


# ── 边界情况 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_content(tmp_path):
    compiled = await compile_forge_md("", str(tmp_path), "m", "k")
    assert compiled.to_constraint_config().architecture.layers == []
    assert compiled.to_permission_rules() == {"deny": [], "ask": [], "allow": []}


def test_parse_json_loose_handles_fences():
    wrapped = '```json\n{"a": 1}\n```'
    assert _parse_json_loose(wrapped) == {"a": 1}

    wrapped2 = 'Here is the result:\n```\n{"b": 2}\n```\nDone.'
    assert _parse_json_loose(wrapped2) == {"b": 2}

    raw = '{"c": 3}'
    assert _parse_json_loose(raw) == {"c": 3}


def test_parse_json_loose_extracts_embedded():
    surrounded = 'Some preamble here {"d": 4} and trailing text'
    assert _parse_json_loose(surrounded) == {"d": 4}


# ── 下游消费兼容性 ──────────────────────────────────────────────────────

def test_compiled_integrates_with_import_checker(tmp_path):
    from forge.constraints.import_checker import check_file

    compiled = CompiledForgeMD({
        "source_hash": "x",
        "cache_version": CACHE_VERSION,
        "architecture": {
            "layers": ["types", "repository", "service", "handler"],
            "cross_cutting": ["util"],
        },
    })
    cfg = compiled.to_constraint_config()

    handler_dir = tmp_path / "handler"
    handler_dir.mkdir()
    (handler_dir / "bad.py").write_text(
        "from repository import user_repo\n", encoding="utf-8"
    )
    (tmp_path / "repository").mkdir()

    violations = check_file(handler_dir / "bad.py", tmp_path, cfg.architecture)
    assert len(violations) == 1
    assert "handler" in violations[0].message
    assert "repository" in violations[0].message


def test_compiled_integrates_with_check_permission(tmp_path):
    from forge.core import Session, ToolCall, SessionMode, PermissionDecision
    from forge.core.permission import check_permission

    compiled = CompiledForgeMD({
        "source_hash": "x",
        "cache_version": CACHE_VERSION,
        "permissions": {
            "deny": [{"tool": "bash", "pattern": r"rm\s+-rf", "path": ""}],
            "ask": [{"tool": "git_commit", "pattern": "", "path": ""}],
            "allow": [{"tool": "read_file", "pattern": "", "path": ""}],
        },
    })

    session = Session(
        project_root=str(tmp_path),
        forge_dir=str(tmp_path),
        mode=SessionMode.EXECUTE,
    )
    session.permission_rules = compiled.to_permission_rules()

    tc_bad = ToolCall(id="1", name="bash", arguments={"command": "rm -rf /tmp/foo"})
    assert check_permission(session, tc_bad) == PermissionDecision.DENY

    tc_read = ToolCall(id="2", name="read_file", arguments={"path": "foo.py"})
    assert check_permission(session, tc_read) == PermissionDecision.ALLOW

    tc_commit = ToolCall(id="3", name="git_commit", arguments={"message": "wip"})
    assert check_permission(session, tc_commit) == PermissionDecision.ASK


def test_format_preview_basic():
    compiled = CompiledForgeMD({
        "architecture": {"layers": ["a", "b"], "cross_cutting": ["util"]},
        "permissions": {"deny": [{"tool": "x"}], "ask": [], "allow": []},
        "lint": {"command": "ruff check ."},
        "structural": {"max_file_lines": 300, "max_function_lines": 40},
        "conventions": ["rule 1", "rule 2"],
    })
    out = format_compiled_preview(compiled)
    assert "ruff" in out
    assert "Conventions" in out or "rule" in out.lower()
