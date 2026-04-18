"""
entropy.py — 启动时熵检测与清理

每次会话启动时执行一次：
1. 清理过期 tool outputs（>7天）
2. 归档已完成的 features（>20个 done）
3. 质量问题数量警告
4. 清理过期 trace 文件（>7天，需已提炼或无评审数据）
5. 检查 experience 文件总量，超过阈值时触发合并
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.harness.entropy")

TOOL_OUTPUT_RETENTION_DAYS = 7
DONE_FEATURES_ARCHIVE_THRESHOLD = 20


def startup_check(session: Session) -> None:
    """每次会话启动时执行一次。"""
    _cleanup_expired_tool_outputs(session)
    _archive_done_features(session)
    _check_quality_grades(session)
    _cleanup_expired_traces(session)
    # experience 合并是异步的，由 observability 层在事件循环启动后调度
    # 这里只做同步的预检查，打印警告
    _warn_if_experience_large(session)


def _cleanup_expired_tool_outputs(session: Session) -> None:
    """删除超过 7 天的 tool output 文件。"""
    output_dir = Path(session.forge_dir) / "tool_outputs"
    if not output_dir.exists():
        return

    cutoff = datetime.now() - timedelta(days=TOOL_OUTPUT_RETENTION_DAYS)
    count = 0
    for f in output_dir.iterdir():
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                count += 1
        except Exception as e:
            logger.debug("Failed to check/delete %s: %s", f, e)

    if count > 0:
        logger.info("清理了 %d 个过期的 tool output 文件", count)


def _archive_done_features(session: Session) -> None:
    """
    如果 features.json 中 done 状态的 feature 超过阈值，
    将它们移到 features_archive.json。
    """
    features_path = Path(session.forge_dir) / "features.json"
    if not features_path.exists():
        return

    try:
        data = json.loads(features_path.read_text(encoding="utf-8"))
    except Exception:
        return

    features = data.get("features", [])
    done_features = [f for f in features if f.get("status") == "done"]
    if len(done_features) <= DONE_FEATURES_ARCHIVE_THRESHOLD:
        return

    # 将 done features 移到归档文件
    archive_path = Path(session.forge_dir) / "features_archive.json"
    existing_archive: list = []
    if archive_path.exists():
        try:
            existing_archive = json.loads(
                archive_path.read_text(encoding="utf-8")
            ).get("archived", [])
        except Exception:
            pass

    existing_archive.extend(done_features)
    try:
        archive_path.write_text(
            json.dumps({"archived": existing_archive}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to write features_archive.json: %s", e)
        return

    # 从主 features 中移除 done 的
    data["features"] = [f for f in features if f.get("status") != "done"]
    try:
        features_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("已归档 %d 个已完成的 feature", len(done_features))
    except Exception as e:
        logger.warning("Failed to update features.json after archiving: %s", e)

    # 更新 session 中的 features_data
    session.features_data = data


def _check_quality_grades(session: Session) -> None:
    """检查 quality_grades.md 中未解决问题的数量。"""
    qg_path = Path(session.project_root) / "docs" / "quality_grades.md"
    if not qg_path.exists():
        return

    try:
        content = qg_path.read_text(encoding="utf-8")
    except Exception:
        return

    # 粗略计数：在 "## Unresolved Issues" 和 "## Resolved Issues" 之间的列表项
    unresolved_section = ""
    match = re.search(
        r"## Unresolved Issues\s*\n(.*?)(?=## Resolved Issues|\Z)",
        content,
        re.DOTALL,
    )
    if match:
        unresolved_section = match.group(1)

    # 计数非空行（排除 "(none yet)"）
    lines = [
        l.strip()
        for l in unresolved_section.splitlines()
        if l.strip() and l.strip() != "(none yet)" and not l.strip().startswith("#")
    ]
    count = len(lines)

    if count > 10:
        print(
            f"\n  ⚠  有 {count} 个待处理的质量问题，"
            f"建议在新会话中让 Forge 处理\n"
        )


def _cleanup_expired_traces(session: Session) -> None:
    """
    清理过期的 trace 文件（>7 天）。

    委托给 experience 模块的 cleanup_expired_traces，
    该函数会确保已提炼过（有 .done sidecar）或无评审数据的文件才被删除。
    """
    try:
        from ..observability.experience import cleanup_expired_traces
        cleanup_expired_traces(session.forge_dir, retention_days=TOOL_OUTPUT_RETENTION_DAYS)
    except Exception as e:
        logger.debug("Trace cleanup failed: %s", e)


def _warn_if_experience_large(session: Session) -> None:
    """
    检查 experience 文件总量。
    如果超过阈值，打印一条提示（实际合并由 observability 层异步执行）。
    """
    from ..observability.experience import EXPERIENCE_DIR_NAME, EXPERIENCE_MERGE_THRESHOLD
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    if not exp_dir.exists():
        return

    total = sum(
        f.stat().st_size
        for f in exp_dir.glob("exp_*.md")
        if f.is_file()
    )
    if total > EXPERIENCE_MERGE_THRESHOLD:
        logger.debug(
            "Experience files total %d chars > threshold, merge will be scheduled",
            total,
        )
