"""
forge_md_compiler.py — FORGE.md 自然语言编译器

核心思想：
- 用户用自然语言写 FORGE.md，无需掌握 YAML 语法
- 首次启动时调用 LLM 把自然语言规则编译为结构化 JSON
- 按源文件 sha256 缓存到 .forge/forge_md.compiled.json
- 下次启动若 hash 不变，直接读缓存（零 LLM 调用开销）
- LLM 调用失败时回退到现有的 YAML 正则解析器（渐进增强）

输出的结构化字段与现有 rules_parser.ConstraintConfig +
permission.PermissionRule 兼容，下游模块无需修改。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session
    from .rules_parser import ConstraintConfig
    from ..core.permission import PermissionRule

logger = logging.getLogger("forge.constraints.forge_md_compiler")

CACHE_FILENAME = "forge_md.compiled.json"
CACHE_VERSION = 1  # 如果 schema 变了，bump 这个版本号，旧缓存会被忽略


# ── 编译结果数据容器 ────────────────────────────────────────────────────

class CompiledForgeMD:
    """
    编译后的 FORGE.md。同时提供两个下游模块需要的格式。
    """

    def __init__(self, data: dict):
        self.data = data
        self.source_hash: str = data.get("source_hash", "")
        self.cache_version: int = data.get("cache_version", 0)

    def to_constraint_config(self) -> ConstraintConfig:
        """转换为 rules_parser.ConstraintConfig。"""
        from .rules_parser import (
            ArchitectureRules, LintConfig, StructuralRule, ConstraintConfig,
        )

        arch_data = self.data.get("architecture") or {}
        lint_data = self.data.get("lint") or {}
        st_data = self.data.get("structural") or {}

        return ConstraintConfig(
            architecture=ArchitectureRules(
                layers=arch_data.get("layers") or [],
                cross_cutting=arch_data.get("cross_cutting") or [],
            ),
            lint=LintConfig(
                command=lint_data.get("command") or "",
                auto_fix_command=lint_data.get("auto_fix_command") or "",
            ),
            structural=StructuralRule(
                max_file_lines=st_data.get("max_file_lines", 500),
                max_function_lines=st_data.get("max_function_lines", 80),
                naming_files=(st_data.get("naming") or {}).get("files", "snake_case"),
                naming_classes=(st_data.get("naming") or {}).get("classes", "PascalCase"),
                forbidden_patterns=st_data.get("forbidden_patterns") or [],
            ),
        )

    def to_permission_rules(self) -> dict[str, list[PermissionRule]]:
        """转换为 permission.check_permission 需要的字典。"""
        from ..core.permission import PermissionRule

        perms = self.data.get("permissions") or {}
        result: dict[str, list[PermissionRule]] = {"deny": [], "ask": [], "allow": []}
        for level in ("deny", "ask", "allow"):
            for item in perms.get(level) or []:
                if not isinstance(item, dict) or "tool" not in item:
                    continue
                result[level].append(PermissionRule(
                    tool=item.get("tool", ""),
                    pattern=item.get("pattern", "") or "",
                    path=item.get("path", "") or "",
                ))
        return result

    def mcp_servers(self) -> list[dict]:
        return self.data.get("mcp_servers") or []

    def conventions(self) -> list[str]:
        return self.data.get("conventions") or []


# ── 主入口 ──────────────────────────────────────────────────────────────

async def compile_forge_md(
    forge_md_content: str,
    forge_dir: str,
    aux_model: str,
    api_key: str,
) -> CompiledForgeMD:
    """
    主入口：编译 FORGE.md 为结构化规则。

    决策树：
    1. 内容为空 → 返回空 CompiledForgeMD
    2. 缓存命中（同 hash + 同 version）→ 读缓存
    3. 调用 LLM 编译，写缓存
    4. LLM 失败 → 降级到 YAML 正则解析（现有逻辑）

    注意：此函数是 async 的。cli.py 需要在 asyncio.run 上下文中调用。
    """
    if not forge_md_content:
        return CompiledForgeMD({"source_hash": "", "cache_version": CACHE_VERSION})

    source_hash = _hash(forge_md_content)
    cache_path = Path(forge_dir) / CACHE_FILENAME

    # 1. 试缓存
    cached = _try_load_cache(cache_path, source_hash)
    if cached is not None:
        logger.debug("FORGE.md cache hit (hash=%s)", source_hash[:8])
        return cached

    # 2. LLM 编译
    logger.info("FORGE.md cache miss, compiling via LLM (hash=%s)", source_hash[:8])
    compiled_data = await _compile_via_llm(forge_md_content, aux_model, api_key)

    if compiled_data is None:
        # 3. 降级：使用现有的正则+YAML 解析器
        logger.warning("LLM compile failed, falling back to legacy YAML parser")
        return _compile_via_legacy_parser(forge_md_content, source_hash)

    # 4. 标记并写缓存
    compiled_data["source_hash"] = source_hash
    compiled_data["cache_version"] = CACHE_VERSION
    _save_cache(cache_path, compiled_data)

    return CompiledForgeMD(compiled_data)


def try_load_cache_only(
    forge_md_content: str,
    forge_dir: str,
) -> CompiledForgeMD | None:
    """
    快速路径：只尝试缓存命中，不调用 LLM。
    用于需要同步完成的场景（如：main 刚启动时，还没进入 await 状态）。
    缓存未命中返回 None，调用方应再走 compile_forge_md（async）。
    """
    if not forge_md_content:
        return CompiledForgeMD({"source_hash": "", "cache_version": CACHE_VERSION})
    source_hash = _hash(forge_md_content)
    cache_path = Path(forge_dir) / CACHE_FILENAME
    return _try_load_cache(cache_path, source_hash)


# ── 缓存读写 ────────────────────────────────────────────────────────────

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _try_load_cache(cache_path: Path, expected_hash: str) -> CompiledForgeMD | None:
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("cache_version") != CACHE_VERSION:
        return None
    if data.get("source_hash") != expected_hash:
        return None
    return CompiledForgeMD(data)


def _save_cache(cache_path: Path, data: dict) -> None:
    tmp = cache_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_path)
    except Exception as e:
        logger.warning("Failed to write cache: %s", e)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ── LLM 编译 ────────────────────────────────────────────────────────────

_COMPILE_SYSTEM = (
    "You are a project-rules compiler. Convert natural-language project rules "
    "into a strict JSON structure. Output ONLY the JSON object, no prose, no "
    "markdown fences. Use exactly the field names and canonical values listed "
    "in the user's instructions."
)

_COMPILE_PROMPT = """Convert the following project rules document into a JSON object
with exactly this schema. Omit keys whose content the document doesn't mention.
Do not invent rules. Do not add fields not listed in the schema.

SCHEMA
{{
  "architecture": {{
    "layers": [string],            // bottom-to-top, e.g. ["types","config","repository","service","handler"]
    "cross_cutting": [string]      // layer names that can be imported from anywhere, e.g. ["util","common"]
  }},
  "permissions": {{
    "deny":  [{{"tool": string, "pattern": string, "path": string}}],
    "ask":   [{{"tool": string, "pattern": string, "path": string}}],
    "allow": [{{"tool": string, "pattern": string, "path": string}}]
  }},
  "lint": {{
    "command": string,             // e.g. "ruff check ."
    "auto_fix_command": string     // e.g. "ruff check --fix ."
  }},
  "structural": {{
    "max_file_lines": integer,
    "max_function_lines": integer,
    "naming": {{"files": "snake_case"|"camelCase"|"PascalCase", "classes": "PascalCase"|"camelCase"}},
    "forbidden_patterns": [{{"pattern": string, "message": string, "exclude": [string]}}]
  }},
  "conventions": [string],         // free-text rules for the agent
  "mcp_servers": [{{"name": string, "command": string, "args": [string], "url": string}}]
}}

CANONICAL TOOL NAMES (use these exact strings in "tool" fields):
- read_file, edit_file, bash, search_code, list_directory
- git_status, git_diff, git_log, git_commit
- Use "*" to match all tools

PATTERN AND PATH fields:
- "pattern" is a Python regex matched against the serialized JSON of the tool's arguments
- "path" is a Python regex matched against path-like argument fields
- Either can be empty string if not specified

INTERPRETATION RULES:
- If the user says a file/command is "forbidden" or "never" → put in "deny"
- If "ask first" or "confirm" → put in "ask"
- If "always allowed" or "pre-approved" or "read-only" → put in "allow"
- Extract escape characters faithfully: "rm -rf" → regex "rm\\\\s+-rf"
- If a rule is stated as a positive fact ("use logging instead of print"),
  the corresponding forbidden_pattern is the thing to avoid ("print\\\\(")
- If the user lists architectural layers in prose, preserve order
  (bottom-up: the layer mentioned first as "lowest" goes first)
- "Conventions" are free-text rules that don't fit other sections,
  like "read a file before editing it" or "run tests after changes"

DOCUMENT:
---
{document}
---

Output the JSON now:"""


async def _compile_via_llm(
    forge_md_content: str,
    aux_model: str,
    api_key: str,
) -> dict | None:
    """调用 LLM 把自然语言规则编译成 JSON。失败返回 None。"""
    if not api_key:
        logger.debug("No API key, skipping LLM compile")
        return None

    try:
        from ..core.query import query_llm
    except ImportError:
        return None

    prompt = _COMPILE_PROMPT.format(document=forge_md_content)

    try:
        response = await query_llm(
            model=aux_model,
            api_key=api_key,
            system_prompt=_COMPILE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tool_definitions=[],
            max_output_tokens=4000,
        )
        raw = response.text
    except Exception as e:
        logger.warning("LLM compile call failed: %s", e)
        return None

    return _parse_json_loose(raw)


def _parse_json_loose(raw: str) -> dict | None:
    """
    容错 JSON 解析。与 initializer._parse_features_json 同策略。
    """
    text = raw.strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None

    if not isinstance(data, dict):
        return None
    return data


# ── Legacy 回退 ─────────────────────────────────────────────────────────

def _compile_via_legacy_parser(
    forge_md_content: str,
    source_hash: str,
) -> CompiledForgeMD:
    """
    当 LLM 不可用时，回退到现有的 YAML+正则解析。
    这保证了向后兼容：已经用 YAML 格式写的 FORGE.md 仍然工作。
    """
    from .rules_parser import parse_constraints
    from ..core.permission import load_permission_rules

    config = parse_constraints(forge_md_content)
    perms = load_permission_rules(forge_md_content)

    data: dict = {
        "source_hash": source_hash,
        "cache_version": CACHE_VERSION,
        "_compiled_by": "legacy_yaml_parser",
        "architecture": {
            "layers": config.architecture.layers,
            "cross_cutting": config.architecture.cross_cutting,
        },
        "lint": {
            "command": config.lint.command,
            "auto_fix_command": config.lint.auto_fix_command,
        },
        "structural": {
            "max_file_lines": config.structural.max_file_lines,
            "max_function_lines": config.structural.max_function_lines,
            "naming": {
                "files": config.structural.naming_files,
                "classes": config.structural.naming_classes,
            },
            "forbidden_patterns": config.structural.forbidden_patterns,
        },
        "permissions": {
            level: [
                {"tool": r.tool, "pattern": r.pattern, "path": r.path}
                for r in rules
            ]
            for level, rules in perms.items()
        },
    }

    # 额外尝试提取 mcp_servers（现有逻辑散落在 constraints/__init__.py 里）
    import yaml  # noqa: deferred import
    try:
        yaml_blocks = re.findall(r"```yaml\s*\n(.*?)```", forge_md_content, re.DOTALL)
        mcp_list: list[dict] = []
        for block in yaml_blocks:
            try:
                y = yaml.safe_load(block)
            except Exception:
                continue
            if isinstance(y, dict) and isinstance(y.get("mcp_servers"), list):
                mcp_list.extend(y["mcp_servers"])
        if mcp_list:
            data["mcp_servers"] = mcp_list
    except Exception:
        pass

    return CompiledForgeMD(data)


# ── 预览辅助函数（给 CLI 确认步骤用）────────────────────────────────────

def format_compiled_preview(compiled: CompiledForgeMD) -> str:
    """格式化为可读摘要，供用户在首次编译时确认。"""
    d = compiled.data
    lines: list[str] = []

    arch = d.get("architecture") or {}
    if arch.get("layers"):
        chain = " → ".join(reversed(arch["layers"]))
        lines.append(f"  Architecture : {chain}")
    if arch.get("cross_cutting"):
        lines.append(f"  Cross-cutting: {', '.join(arch['cross_cutting'])}")

    perms = d.get("permissions") or {}
    perm_counts = {level: len(perms.get(level) or []) for level in ("deny", "ask", "allow")}
    if any(perm_counts.values()):
        lines.append(
            f"  Permissions  : {perm_counts['deny']} deny, "
            f"{perm_counts['ask']} ask, {perm_counts['allow']} allow"
        )

    lint = d.get("lint") or {}
    if lint.get("command"):
        lines.append(f"  Lint command : {lint['command']}")

    st = d.get("structural") or {}
    if st:
        lines.append(
            f"  Structural   : max_file={st.get('max_file_lines', 500)} lines, "
            f"max_function={st.get('max_function_lines', 80)} lines"
        )
        if st.get("forbidden_patterns"):
            lines.append(f"                 {len(st['forbidden_patterns'])} forbidden patterns")

    convs = d.get("conventions") or []
    if convs:
        lines.append(f"  Conventions  : {len(convs)} rules")
        for c in convs[:3]:
            lines.append(f"                 • {c}")
        if len(convs) > 3:
            lines.append(f"                 ... and {len(convs) - 3} more")

    mcp = d.get("mcp_servers") or []
    if mcp:
        lines.append(f"  MCP servers  : {', '.join(s.get('name', '?') for s in mcp)}")

    if d.get("_compiled_by") == "legacy_yaml_parser":
        lines.append("  (parsed via legacy YAML parser — LLM compile unavailable)")

    return "\n".join(lines) if lines else "  (empty — no rules parsed)"
