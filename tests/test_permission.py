"""
test_permission.py — 权限管线测试

覆盖 deny/ask/allow 规则匹配、优先级、默认规则和 FORGE.md 解析。
"""

from __future__ import annotations

import pytest

from forge.core import Session, ToolCall, PermissionDecision
from forge.core.permission import (
    PermissionRule,
    check_permission,
    load_permission_rules,
    DEFAULT_DENY_RULES,
    DEFAULT_ASK_RULES,
    DEFAULT_ALLOW_RULES,
    _rule_matches,
)


@pytest.fixture
def session(tmp_path):
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    s = Session(
        project_root=str(tmp_path),
        forge_dir=str(forge_dir),
    )
    # 使用默认规则
    s.permission_rules = {
        "deny": list(DEFAULT_DENY_RULES),
        "ask": list(DEFAULT_ASK_RULES),
        "allow": list(DEFAULT_ALLOW_RULES),
    }
    return s


# ── rule_matches 单元测试 ────────────────────────────────────────────────

class TestRuleMatches:
    def test_exact_tool_match(self):
        rule = PermissionRule(tool="bash")
        assert _rule_matches(rule, "bash", {}) is True
        assert _rule_matches(rule, "read_file", {}) is False

    def test_wildcard_tool(self):
        rule = PermissionRule(tool="*")
        assert _rule_matches(rule, "bash", {}) is True
        assert _rule_matches(rule, "anything", {}) is True

    def test_pattern_match(self):
        rule = PermissionRule(tool="bash", pattern=r"rm\s+-rf")
        assert _rule_matches(rule, "bash", {"command": "rm -rf /"}) is True
        assert _rule_matches(rule, "bash", {"command": "ls -la"}) is False

    def test_path_match(self):
        rule = PermissionRule(tool="edit_file", path=r"\.lock$")
        assert _rule_matches(rule, "edit_file", {"path": "package-lock.json"}) is False
        assert _rule_matches(rule, "edit_file", {"path": "yarn.lock"}) is True

    def test_path_match_env(self):
        rule = PermissionRule(tool="edit_file", path=r"\.env$")
        assert _rule_matches(rule, "edit_file", {"path": ".env"}) is True
        assert _rule_matches(rule, "edit_file", {"path": "config.py"}) is False

    def test_pattern_and_path_both_required(self):
        """pattern 和 path 同时存在时，两者都要匹配。"""
        rule = PermissionRule(tool="bash", pattern=r"cat", path=r"\.env$")
        # pattern 匹配但 path 不在 command 中
        assert _rule_matches(rule, "bash", {"command": "cat foo.py"}) is False
        # 都匹配（command 字段同时用于 path 匹配）
        assert _rule_matches(rule, "bash", {"command": "cat .env"}) is True

    def test_empty_pattern_matches_all(self):
        rule = PermissionRule(tool="read_file")
        assert _rule_matches(rule, "read_file", {"path": "anything"}) is True


# ── check_permission 集成测试 ────────────────────────────────────────────

class TestCheckPermission:
    def test_deny_rm_rf(self, session):
        """deny 规则拦截 rm -rf 命令。"""
        tc = ToolCall(id="1", name="bash", arguments={"command": "rm -rf /"})
        assert check_permission(session, tc) == PermissionDecision.DENY

    def test_deny_sudo(self, session):
        """deny 规则拦截 sudo 命令。"""
        tc = ToolCall(id="2", name="bash", arguments={"command": "sudo apt install foo"})
        assert check_permission(session, tc) == PermissionDecision.DENY

    def test_deny_lock_file(self, session):
        """deny 规则拦截 .lock 文件编辑。"""
        tc = ToolCall(id="3", name="edit_file", arguments={"path": "yarn.lock", "old_str": "x", "new_str": "y"})
        assert check_permission(session, tc) == PermissionDecision.DENY

    def test_deny_env_file(self, session):
        """deny 规则拦截 .env 文件编辑。"""
        tc = ToolCall(id="4", name="edit_file", arguments={"path": ".env", "old_str": "x", "new_str": "y"})
        assert check_permission(session, tc) == PermissionDecision.DENY

    def test_ask_bash(self, session):
        """ask 规则匹配普通 bash 命令。"""
        tc = ToolCall(id="5", name="bash", arguments={"command": "ls -la"})
        assert check_permission(session, tc) == PermissionDecision.ASK

    def test_ask_git_commit(self, session):
        """ask 规则匹配 git_commit。"""
        tc = ToolCall(id="6", name="git_commit", arguments={"message": "test"})
        assert check_permission(session, tc) == PermissionDecision.ASK

    def test_allow_read_file(self, session):
        """allow 规则匹配 read_file。"""
        tc = ToolCall(id="7", name="read_file", arguments={"path": "main.py"})
        assert check_permission(session, tc) == PermissionDecision.ALLOW

    def test_allow_search_code(self, session):
        tc = ToolCall(id="8", name="search_code", arguments={"pattern": "def main"})
        assert check_permission(session, tc) == PermissionDecision.ALLOW

    def test_allow_list_directory(self, session):
        tc = ToolCall(id="9", name="list_directory", arguments={})
        assert check_permission(session, tc) == PermissionDecision.ALLOW

    def test_allow_git_status(self, session):
        tc = ToolCall(id="10", name="git_status", arguments={})
        assert check_permission(session, tc) == PermissionDecision.ALLOW

    def test_unknown_tool_defaults_to_ask(self, session):
        """无匹配规则默认返回 ASK。"""
        tc = ToolCall(id="11", name="unknown_tool", arguments={"foo": "bar"})
        assert check_permission(session, tc) == PermissionDecision.ASK

    def test_deny_overrides_allow(self, session):
        """deny 优先于 allow：同一工具同时出现在 deny 和 allow 中。"""
        # 添加 read_file 到 deny（rm -rf pattern 虽然不匹配 read_file 但这里测试显式 deny）
        session.permission_rules["deny"].append(PermissionRule(tool="read_file"))
        tc = ToolCall(id="12", name="read_file", arguments={"path": "main.py"})
        assert check_permission(session, tc) == PermissionDecision.DENY


# ── load_permission_rules 测试 ───────────────────────────────────────────

class TestLoadPermissionRules:
    def test_empty_content_returns_defaults(self):
        rules = load_permission_rules("")
        assert len(rules["deny"]) == len(DEFAULT_DENY_RULES)
        assert len(rules["ask"]) == len(DEFAULT_ASK_RULES)
        assert len(rules["allow"]) == len(DEFAULT_ALLOW_RULES)

    def test_invalid_yaml_returns_defaults(self):
        rules = load_permission_rules("not yaml at all [[[")
        assert len(rules["deny"]) == len(DEFAULT_DENY_RULES)

    def test_parse_valid_permissions(self):
        content = '''
Some markdown text.

```yaml
permissions:
  deny:
    - tool: bash
      pattern: "danger"
  ask:
    - tool: custom_tool
  allow:
    - tool: safe_tool
```

More text.
'''
        rules = load_permission_rules(content)
        assert len(rules["deny"]) == 1
        assert rules["deny"][0].tool == "bash"
        assert rules["deny"][0].pattern == "danger"
        assert len(rules["ask"]) == 1
        assert rules["ask"][0].tool == "custom_tool"
        assert len(rules["allow"]) == 1
        assert rules["allow"][0].tool == "safe_tool"

    def test_yaml_without_permissions_key_returns_defaults(self):
        content = '''
```yaml
architecture:
  layers:
    - types
    - service
```
'''
        rules = load_permission_rules(content)
        assert len(rules["deny"]) == len(DEFAULT_DENY_RULES)

    def test_forge_md_parse_failure_returns_defaults(self):
        """FORGE.md 解析失败时使用默认规则。"""
        content = '''
```yaml
permissions:
  deny:
    - this is not valid: [
```
'''
        rules = load_permission_rules(content)
        # yaml.safe_load should raise, fall through to defaults
        assert len(rules["deny"]) == len(DEFAULT_DENY_RULES)
