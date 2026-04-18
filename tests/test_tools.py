"""
test_tools.py — 内置工具层测试

覆盖：read_file / edit_file / bash / search_code / list_directory
以及工具输出截断和 recently_read_files 追踪机制。
"""

from __future__ import annotations

import asyncio
import pytest
from pathlib import Path

from forge.core import Session, SessionMode, ToolCall, ToolDefinition
from forge.core.permission import PermissionRule
from forge.core.tool_dispatch import ToolDispatch


FIXTURES = Path(__file__).parent / "fixtures" / "dual_project"


@pytest.fixture
def session(tmp_path):
    fd = tmp_path / ".forge"
    fd.mkdir()
    s = Session(
        project_root=str(tmp_path),
        forge_dir=str(fd),
        mode=SessionMode.EXECUTE,
    )
    # 全部允许（权限测试由 test_permission.py 负责）
    s.permission_rules = {"deny": [], "ask": [], "allow": [PermissionRule(tool="*")]}
    ToolDispatch.register_builtin_tools(s)
    return s


@pytest.fixture
def project_session(tmp_path):
    """以 dual_project fixture 为 project_root 的 session。"""
    import shutil
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURES, dst)
    fd = tmp_path / ".forge"
    fd.mkdir()
    s = Session(
        project_root=str(dst),
        forge_dir=str(fd),
        mode=SessionMode.EXECUTE,
    )
    s.permission_rules = {"deny": [], "ask": [], "allow": [PermissionRule(tool="*")]}
    ToolDispatch.register_builtin_tools(s)
    return s


# ── read_file ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_file_success(project_session):
    """读取存在的文件，返回带行号的内容。"""
    tc = ToolCall(id="t1", name="read_file", arguments={"path": "util/helpers.py"})
    result = await ToolDispatch.execute(project_session, tc)
    assert not result.is_error
    assert "hash_password" in result.content
    assert "1\t" in result.content or "1 " in result.content  # 有行号


@pytest.mark.asyncio
async def test_read_file_not_found(session):
    """读取不存在的文件返回错误。"""
    (Path(session.project_root) / "dummy.py").parent.mkdir(parents=True, exist_ok=True)
    tc = ToolCall(id="t1", name="read_file", arguments={"path": "nonexistent.py"})
    result = await ToolDispatch.execute(session, tc)
    assert result.is_error


@pytest.mark.asyncio
async def test_read_file_outside_project(session):
    """路径逃逸到项目外被拒绝。"""
    tc = ToolCall(id="t1", name="read_file", arguments={"path": "../../../etc/passwd"})
    result = await ToolDispatch.execute(session, tc)
    assert result.is_error


@pytest.mark.asyncio
async def test_read_file_line_range(project_session):
    """带 line_range 参数只返回指定行范围。"""
    tc = ToolCall(id="t1", name="read_file", arguments={
        "path": "util/helpers.py",
        "line_range": [1, 3],
    })
    result = await ToolDispatch.execute(project_session, tc)
    assert not result.is_error
    lines = result.content.strip().splitlines()
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_read_file_tracks_recently_read(session):
    """成功读取文件后路径追加到 recently_read_files。"""
    target = Path(session.project_root) / "foo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    tc = ToolCall(id="t1", name="read_file", arguments={"path": "foo.py"})
    result = await ToolDispatch.execute(session, tc)
    assert not result.is_error
    assert any("foo.py" in p for p in session.recently_read_files)


# ── edit_file ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_edit_file_create_new(session):
    """old_str 为空 → 创建新文件。"""
    tc = ToolCall(id="t1", name="edit_file", arguments={
        "path": "new_module.py",
        "old_str": "",
        "new_str": "def hello():\n    return 'hello'\n",
    })
    result = await ToolDispatch.execute(session, tc)
    assert not result.is_error
    assert (Path(session.project_root) / "new_module.py").exists()


@pytest.mark.asyncio
async def test_edit_file_str_replace(session):
    """old_str 唯一存在 → 精确替换。"""
    target = Path(session.project_root) / "sample.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")
    tc = ToolCall(id="t1", name="edit_file", arguments={
        "path": "sample.py",
        "old_str": "return 1",
        "new_str": "return 42",
    })
    result = await ToolDispatch.execute(session, tc)
    assert not result.is_error
    assert "return 42" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_edit_file_not_found_old_str(session):
    """old_str 不在文件中 → is_error=True。"""
    target = Path(session.project_root) / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")
    tc = ToolCall(id="t1", name="edit_file", arguments={
        "path": "sample.py",
        "old_str": "DOES_NOT_EXIST",
        "new_str": "anything",
    })
    result = await ToolDispatch.execute(session, tc)
    assert result.is_error


@pytest.mark.asyncio
async def test_edit_file_ambiguous_old_str(session):
    """old_str 出现多次 → is_error=True。"""
    target = Path(session.project_root) / "dupe.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    tc = ToolCall(id="t1", name="edit_file", arguments={
        "path": "dupe.py",
        "old_str": "x = 1",
        "new_str": "x = 2",
    })
    result = await ToolDispatch.execute(session, tc)
    assert result.is_error
    assert "次" in result.content or "times" in result.content.lower()


@pytest.mark.asyncio
async def test_edit_file_outside_project(session):
    """路径逃逸被拒绝。"""
    tc = ToolCall(id="t1", name="edit_file", arguments={
        "path": "../../evil.py",
        "old_str": "",
        "new_str": "evil",
    })
    result = await ToolDispatch.execute(session, tc)
    assert result.is_error


# ── bash ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_success(session):
    """执行 echo → 返回正确输出。"""
    tc = ToolCall(id="t1", name="bash", arguments={"command": "echo hello_forge"})
    result = await ToolDispatch.execute(session, tc)
    assert not result.is_error
    assert "hello_forge" in result.content


@pytest.mark.asyncio
async def test_bash_timeout(session):
    """执行超时命令 → is_error=True。"""
    tc = ToolCall(id="t1", name="bash", arguments={
        "command": "sleep 10",
        "timeout": 1,
    })
    result = await ToolDispatch.execute(session, tc)
    assert result.is_error


@pytest.mark.asyncio
async def test_bash_nonzero_exit(session):
    """命令退出码非 0 → 内容包含错误信息。"""
    tc = ToolCall(id="t1", name="bash", arguments={
        "command": "ls /path_that_does_not_exist_xyz"
    })
    result = await ToolDispatch.execute(session, tc)
    # bash 工具不一定标记 is_error，但输出中会包含错误信息
    assert "No such file" in result.content or result.is_error


# ── search_code ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_code_found(project_session):
    """搜索存在的关键字 → 返回含文件名的结果。
    因 Windows 上 rg/grep 可能不存在，直接 mock 底层搜索函数。
    """
    from unittest.mock import patch as _patch, AsyncMock as _AM
    import forge.tools.search as _search_mod

    fake_output = "util/helpers.py:4:def hash_password(plain: str) -> str:"

    async def _fake_rg(pattern, search_path, file_pattern, max_count=50):
        return fake_output, True

    async def _fake_grep(pattern, search_path, file_pattern):
        return fake_output, True

    with _patch.object(_search_mod, "_search_rg", side_effect=_fake_rg),          _patch.object(_search_mod, "_search_grep", side_effect=_fake_grep),          _patch.object(_search_mod, "_HAS_RG", True):
        tc = ToolCall(id="t1", name="search_code", arguments={
            "pattern": "hash_password",
            "path": ".",
        })
        result = await ToolDispatch.execute(project_session, tc)

    assert not result.is_error
    assert "helpers" in result.content


@pytest.mark.asyncio
async def test_search_code_not_found(project_session):
    """搜索不存在的关键字 → 返回空或提示。"""
    tc = ToolCall(id="t1", name="search_code", arguments={
        "pattern": "ZZZNOTHINGZZZ",
        "path": ".",
    })
    result = await ToolDispatch.execute(project_session, tc)
    assert not result.is_error
    assert "ZZZNOTHINGZZZ" not in result.content or len(result.content.strip()) == 0


# ── list_directory / glob ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_glob_list_directory(project_session):
    """列出目录 → 树形结构含文件名。"""
    tc = ToolCall(id="t1", name="list_directory", arguments={"path": "."})
    result = await ToolDispatch.execute(project_session, tc)
    assert not result.is_error
    assert "helpers.py" in result.content or "util" in result.content


@pytest.mark.asyncio
async def test_glob_excludes_cache(session):
    """__pycache__ 不出现在结果中。"""
    cache_dir = Path(session.project_root) / "mymod" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "foo.pyc").write_bytes(b"")
    tc = ToolCall(id="t1", name="list_directory", arguments={"path": "."})
    result = await ToolDispatch.execute(session, tc)
    assert "__pycache__" not in result.content


# ── 工具输出截断 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_output_truncation(session):
    """工具输出超过 max_result_chars → 截断并写入 .forge/tool_outputs/。"""
    # 创建一个大文件
    big_file = Path(session.project_root) / "big.txt"
    big_file.write_text("x" * 35000, encoding="utf-8")

    tc = ToolCall(id="t1", name="read_file", arguments={"path": "big.txt"})
    result = await ToolDispatch.execute(session, tc)

    output_dir = Path(session.forge_dir) / "tool_outputs"
    saved_files = list(output_dir.glob("read_file_*.txt")) if output_dir.exists() else []
    assert len(saved_files) >= 1 or "truncated" in result.content.lower()
