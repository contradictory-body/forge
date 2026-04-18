"""
rules_parser.py — 从 FORGE.md 解析架构约束、lint 配置、结构性规则。

查找 FORGE.md 中的 ```yaml 块，提取 architecture / lint / structural 键。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("forge.constraints.rules_parser")


@dataclass
class ArchitectureRules:
    """架构层级规则。layers 从底到顶排列。"""
    layers: list[str] = field(default_factory=list)
    cross_cutting: list[str] = field(default_factory=list)


@dataclass
class LintConfig:
    """项目 linter 配置。"""
    command: str = ""
    auto_fix_command: str = ""


@dataclass
class StructuralRule:
    """结构性约束规则。"""
    max_file_lines: int = 500
    max_function_lines: int = 80
    naming_files: str = "snake_case"
    naming_classes: str = "PascalCase"
    forbidden_patterns: list[dict] = field(default_factory=list)


@dataclass
class ConstraintConfig:
    """完整的约束配置。"""
    architecture: ArchitectureRules = field(default_factory=ArchitectureRules)
    lint: LintConfig = field(default_factory=LintConfig)
    structural: StructuralRule = field(default_factory=StructuralRule)


def parse_constraints(forge_md_content: str) -> ConstraintConfig:
    """
    从 FORGE.md 内容中解析约束配置。
    解析失败时返回默认 ConstraintConfig。
    """
    if not forge_md_content:
        return ConstraintConfig()

    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed, using default constraints")
        return ConstraintConfig()

    yaml_blocks = re.findall(r"```yaml\s*\n(.*?)```", forge_md_content, re.DOTALL)
    if not yaml_blocks:
        return ConstraintConfig()

    config = ConstraintConfig()

    for block in yaml_blocks:
        try:
            data = yaml.safe_load(block)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        # architecture
        if "architecture" in data:
            arch = data["architecture"]
            if isinstance(arch, dict):
                config.architecture = ArchitectureRules(
                    layers=arch.get("layers", []) or [],
                    cross_cutting=arch.get("cross_cutting", []) or [],
                )

        # lint
        if "lint" in data:
            lint = data["lint"]
            if isinstance(lint, dict):
                config.lint = LintConfig(
                    command=lint.get("command", "") or "",
                    auto_fix_command=lint.get("auto_fix_command", "") or "",
                )

        # structural
        if "structural" in data:
            st = data["structural"]
            if isinstance(st, dict):
                naming = st.get("naming", {}) or {}
                config.structural = StructuralRule(
                    max_file_lines=st.get("max_file_lines", 500),
                    max_function_lines=st.get("max_function_lines", 80),
                    naming_files=naming.get("files", "snake_case"),
                    naming_classes=naming.get("classes", "PascalCase"),
                    forbidden_patterns=st.get("forbidden_patterns", []) or [],
                )

    logger.info(
        "Parsed constraints: %d layers, lint=%s, %d forbidden patterns",
        len(config.architecture.layers),
        bool(config.lint.command),
        len(config.structural.forbidden_patterns),
    )
    return config
