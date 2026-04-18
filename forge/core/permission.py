"""
permission.py — 三层权限管线

评估顺序：deny → ask → allow。First match wins。
deny 优先于 allow，保证 fail-safe。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("forge.core.permission")


@dataclass
class PermissionRule:
    """单条权限规则。"""
    tool: str              # 工具名称，"*" 表示所有工具
    pattern: str = ""      # 正则匹配工具参数的序列化字符串
    path: str = ""         # 正则匹配文件路径参数


# ── 默认规则 ──────────────────────────────────────────────────────────────

DEFAULT_DENY_RULES = [
    PermissionRule(tool="bash", pattern=r"rm\s+-rf"),
    PermissionRule(tool="bash", pattern=r"sudo"),
    PermissionRule(tool="edit_file", path=r"\.lock$"),
    PermissionRule(tool="edit_file", path=r"\.env$"),
]
DEFAULT_ASK_RULES = [
    PermissionRule(tool="bash"),
    PermissionRule(tool="git_commit"),
]
DEFAULT_ALLOW_RULES = [
    PermissionRule(tool="read_file"),
    PermissionRule(tool="search_code"),
    PermissionRule(tool="list_directory"),
    PermissionRule(tool="git_status"),
    PermissionRule(tool="git_diff"),
    PermissionRule(tool="git_log"),
]


def _make_default_rules() -> dict[str, list[PermissionRule]]:
    return {
        "deny": list(DEFAULT_DENY_RULES),
        "ask": list(DEFAULT_ASK_RULES),
        "allow": list(DEFAULT_ALLOW_RULES),
    }


def load_permission_rules(forge_md_content: str) -> dict[str, list[PermissionRule]]:
    """
    从 FORGE.md 内容中解析权限规则。

    查找 ```yaml ... ``` 块中的 permissions 键。
    解析失败时返回默认规则。
    """
    if not forge_md_content:
        return _make_default_rules()

    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed, using default permission rules")
        return _make_default_rules()

    # 提取所有 yaml 代码块
    yaml_blocks = re.findall(r"```yaml\s*\n(.*?)```", forge_md_content, re.DOTALL)
    if not yaml_blocks:
        return _make_default_rules()

    for block in yaml_blocks:
        try:
            data = yaml.safe_load(block)
        except Exception:
            continue
        if not isinstance(data, dict) or "permissions" not in data:
            continue

        perms = data["permissions"]
        result: dict[str, list[PermissionRule]] = {"deny": [], "ask": [], "allow": []}
        for level in ("deny", "ask", "allow"):
            for item in perms.get(level, []) or []:
                if not isinstance(item, dict) or "tool" not in item:
                    continue
                result[level].append(PermissionRule(
                    tool=item["tool"],
                    pattern=item.get("pattern", ""),
                    path=item.get("path", ""),
                ))
        logger.info("Loaded permission rules from FORGE.md: %s",
                     {k: len(v) for k, v in result.items()})
        return result

    return _make_default_rules()


def _rule_matches(rule: PermissionRule, tool_name: str, arguments: dict) -> bool:
    """检查单条规则是否匹配。"""
    # 1. 工具名匹配
    if rule.tool != "*" and rule.tool != tool_name:
        return False

    # 2. pattern 匹配（对序列化参数）
    if rule.pattern:
        serialized = json.dumps(arguments, ensure_ascii=False)
        if not re.search(rule.pattern, serialized):
            return False

    # 3. path 匹配（从参数中提取路径类字段）
    if rule.path:
        path_value = (
            arguments.get("path", "")
            or arguments.get("file", "")
            or arguments.get("command", "")
        )
        if not path_value or not re.search(rule.path, str(path_value)):
            return False

    return True


def check_permission(session: "Session", tool_call: "ToolCall") -> "PermissionDecision":
    """
    评估工具调用权限。deny → ask → allow → 默认 ASK。
    """
    from . import PermissionDecision

    rules = session.permission_rules

    # deny — first match wins
    for rule in rules.get("deny", []):
        if _rule_matches(rule, tool_call.name, tool_call.arguments):
            logger.info("Permission DENY: tool=%s matched rule=%s", tool_call.name, rule)
            return PermissionDecision.DENY

    # ask
    for rule in rules.get("ask", []):
        if _rule_matches(rule, tool_call.name, tool_call.arguments):
            return PermissionDecision.ASK

    # allow
    for rule in rules.get("allow", []):
        if _rule_matches(rule, tool_call.name, tool_call.arguments):
            return PermissionDecision.ALLOW

    # fail-safe default
    return PermissionDecision.ASK
