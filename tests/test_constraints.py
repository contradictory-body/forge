"""
test_constraints.py — 架构约束引擎测试

覆盖：import_checker, structural_tests, lint_hook, rules_parser
"""

from __future__ import annotations

import pytest
from pathlib import Path

from forge.core import Session, ToolCall, ToolResult, HookResult
from forge.constraints.rules_parser import (
    ArchitectureRules, StructuralRule, ConstraintConfig, LintConfig, parse_constraints,
)
from forge.constraints.import_checker import check_file
from forge.constraints.structural_tests import check_file_structural


FIXTURES = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture
def arch_rules():
    return ArchitectureRules(
        layers=["types", "config", "repository", "service", "handler"],
        cross_cutting=["util", "common"],
    )


@pytest.fixture
def structural_rules():
    return StructuralRule(
        max_file_lines=500, max_function_lines=80,
        naming_files="snake_case", naming_classes="PascalCase",
        forbidden_patterns=[
            {"pattern": r"print\(", "exclude": ["cli.py", "terminal_ui.py"],
             "message": "Use logging instead of print()"},
        ],
    )


@pytest.fixture
def session(tmp_path):
    fd = tmp_path / ".forge"; fd.mkdir()
    return Session(project_root=str(tmp_path), forge_dir=str(fd))


# ── 1. import_checker 基本违规 ───────────────────────────────────────────

class TestImportChecker:

    def test_handler_imports_repository_violation(self, arch_rules):
        """handler 层 import repository → 检测到违规。"""
        violations = check_file(
            FIXTURES / "handler" / "bad_handler.py", FIXTURES, arch_rules
        )
        assert len(violations) >= 1
        v = violations[0]
        assert v.importing_layer == "handler"
        assert v.imported_layer == "repository"
        assert "import_direction" in v.message
        assert "Fix:" in v.message

    # ── 2. 合法 import ──

    def test_handler_imports_service_valid(self, arch_rules):
        """handler import service → 无违规。"""
        violations = check_file(
            FIXTURES / "handler" / "user_handler.py", FIXTURES, arch_rules
        )
        # handler(index=4) importing service(index=3) is valid
        svc_violations = [v for v in violations if v.imported_layer == "service"]
        assert len(svc_violations) == 0

    # ── 3. cross_cutting ──

    def test_cross_cutting_no_violation(self, arch_rules):
        """handler import util (cross_cutting) → 无违规。"""
        # user_handler.py doesn't import util, but the rule should skip cross_cutting
        # Let's create a temp file
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", dir=FIXTURES / "handler", delete=False
        ) as f:
            f.write("from util.helpers import format_name\n")
            tmp_path = Path(f.name)
        try:
            violations = check_file(tmp_path, FIXTURES, arch_rules)
            util_violations = [v for v in violations if "util" in v.imported_module]
            assert len(util_violations) == 0
        finally:
            tmp_path.unlink()

    def test_service_imports_repository_valid(self, arch_rules):
        """service import repository → 合法。"""
        violations = check_file(
            FIXTURES / "service" / "user_service.py", FIXTURES, arch_rules
        )
        repo_violations = [v for v in violations if v.imported_layer == "repository"]
        assert len(repo_violations) == 0

    def test_empty_layers_returns_empty(self):
        rules = ArchitectureRules(layers=[], cross_cutting=[])
        violations = check_file(
            FIXTURES / "handler" / "bad_handler.py", FIXTURES, rules
        )
        assert violations == []


# ── 4-5. structural_tests ────────────────────────────────────────────────

class TestStructuralTests:

    def test_file_too_large(self, tmp_path, structural_rules):
        """600 行文件 → file_size 违规。"""
        big = tmp_path / "big.py"
        big.write_text("\n".join(f"x_{i} = {i}" for i in range(600)))
        violations = check_file_structural(big, tmp_path, structural_rules)
        size_v = [v for v in violations if v.rule == "file_size"]
        assert len(size_v) == 1
        assert "600" in size_v[0].message

    def test_forbidden_pattern_print(self, tmp_path, structural_rules):
        """print( 不在 exclude → 违规。"""
        f = tmp_path / "some_module.py"
        f.write_text('x = 1\nprint("debug")\n')
        violations = check_file_structural(f, tmp_path, structural_rules)
        pv = [v for v in violations if v.rule == "forbidden_pattern"]
        assert len(pv) == 1
        assert "logging" in pv[0].message.lower()

    def test_forbidden_pattern_excluded(self, tmp_path, structural_rules):
        """print( 在 exclude 文件 → 不违规。"""
        f = tmp_path / "cli.py"
        f.write_text('print("ok")\n')
        violations = check_file_structural(f, tmp_path, structural_rules)
        pv = [v for v in violations if v.rule == "forbidden_pattern"]
        assert len(pv) == 0

    def test_function_too_long(self, tmp_path, structural_rules):
        lines = ["def long_func():"]
        for i in range(90):
            lines.append(f"    x_{i} = {i}")
        f = tmp_path / "long.py"
        f.write_text("\n".join(lines))
        violations = check_file_structural(f, tmp_path, structural_rules)
        fv = [v for v in violations if v.rule == "function_size"]
        assert len(fv) == 1
        assert "long_func" in fv[0].message

    def test_clean_file_no_violations(self, tmp_path, structural_rules):
        f = tmp_path / "clean.py"
        f.write_text("import logging\n\ndef hello():\n    logging.info('hi')\n")
        violations = check_file_structural(f, tmp_path, structural_rules)
        assert len(violations) == 0


# ── 6-7. lint_hook ───────────────────────────────────────────────────────

class TestLintHook:

    @pytest.mark.asyncio
    async def test_silent_success(self, session):
        """无违规 → inject_content 为 None。"""
        from forge.constraints.lint_runner import lint_hook, set_config
        set_config(ConstraintConfig(
            structural=StructuralRule(forbidden_patterns=[]),
        ))
        clean = Path(session.project_root) / "clean.py"
        clean.write_text("import logging\n\ndef f():\n    pass\n")
        tc = ToolCall(id="t1", name="edit_file", arguments={"path": "clean.py"})
        tr = ToolResult(content="ok")
        result = await lint_hook(session, tc, tr)
        assert result.inject_content is None

    @pytest.mark.asyncio
    async def test_loud_failure(self, session):
        """有违规 → inject_content 包含错误消息。"""
        from forge.constraints.lint_runner import lint_hook, set_config
        set_config(ConstraintConfig(
            structural=StructuralRule(max_file_lines=5, forbidden_patterns=[]),
        ))
        big = Path(session.project_root) / "big.py"
        big.write_text("\n".join(f"x={i}" for i in range(20)))
        tc = ToolCall(id="t2", name="edit_file", arguments={"path": "big.py"})
        tr = ToolResult(content="ok")
        result = await lint_hook(session, tc, tr)
        assert result.inject_content is not None
        assert "Constraint Check" in result.inject_content
        assert "big.py" in result.inject_content

    @pytest.mark.asyncio
    async def test_skip_on_edit_error(self, session):
        """编辑失败 → 直接返回空 HookResult。"""
        from forge.constraints.lint_runner import lint_hook, set_config
        set_config(ConstraintConfig())
        tc = ToolCall(id="t3", name="edit_file", arguments={"path": "x.py"})
        tr = ToolResult(content="not found", is_error=True)
        result = await lint_hook(session, tc, tr)
        assert result.inject_content is None


# ── rules_parser ─────────────────────────────────────────────────────────

class TestRulesParser:

    def test_parse_valid_forge_md(self):
        content = (FIXTURES / "FORGE.md").read_text(encoding="utf-8")
        config = parse_constraints(content)
        assert config.architecture.layers == [
            "types", "config", "repository", "service", "handler"
        ]
        assert "util" in config.architecture.cross_cutting
        assert config.structural.max_file_lines == 500
        assert len(config.structural.forbidden_patterns) == 1

    def test_empty_returns_defaults(self):
        c = parse_constraints("")
        assert c.architecture.layers == []

    def test_invalid_yaml_returns_defaults(self):
        c = parse_constraints("no yaml here")
        assert c.architecture.layers == []
