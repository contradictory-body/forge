"""pytest conftest — 共享 fixtures。"""

import pytest
from pathlib import Path


@pytest.fixture
def tmp_project(tmp_path):
    """创建一个临时项目目录，含 .forge/ 和一个示例文件。"""
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    return tmp_path
