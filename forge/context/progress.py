"""
progress.py — features.json 和 progress.json 管理

features.json: Initializer Agent 生成的结构化 feature 列表（JSON 格式）
progress.json: 每次会话结束时写出的交接状态
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.context.progress")


# ── 文件读写 ─────────────────────────────────────────────────────────────

def load_features(forge_dir: str) -> dict | None:
    """加载 .forge/features.json，不存在返回 None。"""
    path = Path(forge_dir) / "features.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load features.json: %s", e)
        return None


def load_progress(forge_dir: str) -> dict | None:
    """加载 .forge/progress.json，不存在返回 None。"""
    path = Path(forge_dir) / "progress.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load progress.json: %s", e)
        return None


def save_features(forge_dir: str, features: dict) -> None:
    """保存 features.json。原子写入（先写 .tmp 再 rename）。"""
    path = Path(forge_dir) / "features.json"
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(features, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        logger.debug("Saved features.json")
    except Exception as e:
        logger.error("Failed to save features.json: %s", e)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def save_progress(forge_dir: str, progress: dict) -> None:
    """保存 progress.json。原子写入。"""
    path = Path(forge_dir) / "progress.json"
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(progress, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        logger.debug("Saved progress.json")
    except Exception as e:
        logger.error("Failed to save progress.json: %s", e)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ── Feature 状态管理 ─────────────────────────────────────────────────────

def mark_feature_done(features: dict, feature_id: str, commit_sha: str) -> None:
    """将指定 feature 的 status 改为 done。"""
    for feat in features.get("features", []):
        if feat.get("id") == feature_id:
            feat["status"] = "done"
            feat["completed_at"] = datetime.now().isoformat()
            feat["commit_sha"] = commit_sha
            logger.info("Feature %s marked as done", feature_id)
            return
    logger.warning("Feature %s not found in features list", feature_id)


def get_next_feature(features: dict) -> dict | None:
    """
    获取下一个应该工作的 feature。

    优先级：
    1. status == "in_progress"（继续未完成的）
    2. status == "pending" 且所有 depends_on 都已 done
    3. 全部 done → None
    """
    all_features = features.get("features", [])

    # 构建已完成 id 集合
    done_ids = {f["id"] for f in all_features if f.get("status") == "done"}

    # 1. 找 in_progress
    for feat in all_features:
        if feat.get("status") == "in_progress":
            return feat

    # 2. 找 pending 且依赖已满足
    for feat in all_features:
        if feat.get("status") != "pending":
            continue
        deps = feat.get("depends_on", [])
        if all(d in done_ids for d in deps):
            return feat

    return None


# ── Progress Snapshot ────────────────────────────────────────────────────

def build_progress_snapshot(session: Session) -> dict:
    """
    根据当前会话状态构建 progress.json 的内容。
    """
    # 提取 edit_file 调用的路径
    files_modified: set[str] = set()
    last_test_output = ""
    open_issues: list[str] = []
    last_assistant_text = ""

    for msg in session.messages:
        role = msg.get("role", "")
        content = msg.get("content")

        # 收集编辑过的文件
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") == "edit_file":
                        inp = block.get("input", {})
                        if isinstance(inp, dict) and "path" in inp:
                            files_modified.add(inp["path"])

        # 收集最后一次 test 输出
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tc = block.get("content", "")
                    if isinstance(tc, str) and ("test" in tc.lower() or "pytest" in tc.lower()):
                        last_test_output = tc[:500]
                    if isinstance(tc, str) and block.get("is_error"):
                        open_issues.append(tc[:200])

        # 收集最后 assistant 文本
        if role == "assistant" and isinstance(content, str):
            last_assistant_text = content

    # 当前 feature
    current_feature = ""
    if session.features_data:
        nxt = get_next_feature(session.features_data)
        if nxt:
            current_feature = nxt.get("id", "")

    # git 分支
    git_branch = ""
    git_cache = session.metadata.get("git_status_cache", "")
    if git_cache:
        for line in git_cache.splitlines():
            if line.startswith("Branch:"):
                git_branch = line.split(":", 1)[1].strip()
                break

    # 从最后 assistant 消息提取 next steps（粗略）
    next_steps = ""
    if last_assistant_text:
        # 取最后 200 字符作为 next steps 提示
        next_steps = last_assistant_text[-200:].strip()

    return {
        "last_session": datetime.now().isoformat(),
        "current_feature": current_feature,
        "files_modified": sorted(files_modified),
        "tests_status": {
            "summary": last_test_output[:300] if last_test_output else "no tests run",
        },
        "next_steps": next_steps,
        "git_branch": git_branch,
        "open_issues": open_issues[-5:],  # 最多 5 条
    }
