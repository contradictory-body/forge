"""
assembler.py — 分层 System Prompt 组装器

设计原则：
1. 核心规则每轮都加载（Layer 0-3）
2. 扩展上下文按需注入（Layer 4-6）
3. 每层有 token budget 软限制
4. 总 system prompt 控制在 ~8000 tokens 以内

Layer 2 (Git 状态) 通过 pre_query 钩子异步刷新到
session.metadata["git_status_cache"]，build() 本身是同步函数。

Layer 6 (Agent Self-Knowledge) 从 .forge/experience/ 目录读取，
由 observability/experience.py 异步维护。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.context.assembler")

# ── 每层 token budget ────────────────────────────────────────────────────

LAYER_BUDGETS = {
    0: 600,    # 基础指令
    1: 1500,   # FORGE.md
    2: 400,    # Git 状态
    3: 1000,   # Auto memory
    4: 2000,   # Session memory
    5: 800,    # Progress
    6: 1500,   # Agent Self-Knowledge（经验教训）
}
TOTAL_BUDGET = 8000


def _truncate_to_budget(text: str, budget_tokens: int) -> tuple[str, bool]:
    """
    将文本截断到 budget_tokens 以内。
    返回 (截断后文本, 是否被截断)。
    使用字符数 / 4 近似 token 数以保持同步。
    """
    max_chars = budget_tokens * 4
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


# ── Git 状态刷新（异步，由 pre_query 钩子调用）───────────────────────────

async def refresh_git_status(session: Session) -> None:
    """
    异步刷新 git 状态并缓存到 session.metadata["git_status_cache"]。
    由 init_context_layer 注册为 pre_query 钩子。
    """
    git_dir = Path(session.project_root) / ".git"
    if not git_dir.exists():
        session.metadata["git_status_cache"] = ""
        return

    parts: list[str] = []
    for cmd_args, label in [
        (["git", "branch", "--show-current"], "Branch"),
        (["git", "log", "--oneline", "-5"], "Recent commits"),
        (["git", "diff", "--stat"], "Working tree changes"),
    ]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=session.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            output = stdout.decode("utf-8", errors="replace").strip()
            if output:
                parts.append(f"{label}: {output}")
        except Exception as e:
            logger.debug("Git command %s failed: %s", cmd_args[1], e)

    session.metadata["git_status_cache"] = "\n".join(parts)


# ── System Prompt 组装（同步）────────────────────────────────────────────

def build(session: Session) -> str:
    """
    分层组装完整的 system prompt。

    Layer 0: 基础指令（硬编码）
    Layer 1: FORGE.md 核心规则
    Layer 2: Git 状态（从缓存读取）
    Layer 3: Auto Memory
    Layer 4: Session Memory
    Layer 5: Progress + Features
    Layer 6: Agent Self-Knowledge（从 .forge/experience/ 读取）
    """
    layers: list[str] = []

    # ── Layer 0: 基础指令 ──
    layer0 = (
        f"You are Forge, a terminal-based coding agent.\n"
        f"Current mode: {session.mode.value}\n"
        f"Project root: {session.project_root}\n"
        f"Current date: {date.today().isoformat()}\n"
        f"\n"
        f"Core behavior rules:\n"
        f"- Read a file before editing it (use read_file, not bash cat).\n"
        f"- Use edit_file with str_replace semantics for precise edits.\n"
        f"- After editing, run tests to verify your changes.\n"
        f"- If unsure, use search_code first.\n"
        f"- Prefer specialized tools (read_file, search_code) over bash equivalents.\n"
        f"- Focus on one feature at a time.\n"
        f"- Leave the codebase in a committable state when done."
    )
    layer0, _ = _truncate_to_budget(layer0, LAYER_BUDGETS[0])
    layers.append(layer0)

    # ── Layer 1: FORGE.md ──
    if session.forge_md_content:
        content, truncated = _truncate_to_budget(
            session.forge_md_content, LAYER_BUDGETS[1]
        )
        if truncated:
            content += "\n[FORGE.md truncated. Use read_file to see full content.]"
        layers.append(
            f"\n=== Project Rules (FORGE.md) ===\n{content}\n=== End Project Rules ==="
        )

    # ── Layer 2: Git 状态（从缓存读取）──
    git_cache = session.metadata.get("git_status_cache", "")
    if git_cache:
        git_text, _ = _truncate_to_budget(git_cache, LAYER_BUDGETS[2])
        layers.append(f"\n=== Git Status ===\n{git_text}\n=== End Git Status ===")

    # ── Layer 3: Auto Memory ──
    if session.auto_memory_content:
        content, _ = _truncate_to_budget(
            session.auto_memory_content, LAYER_BUDGETS[3]
        )
        layers.append(
            f"\n=== Auto Memory (cross-session learnings) ===\n"
            f"{content}\n=== End Auto Memory ==="
        )

    # ── Layer 4: Session Memory ──
    if session.session_memory_content:
        content, _ = _truncate_to_budget(
            session.session_memory_content, LAYER_BUDGETS[4]
        )
        layers.append(
            f"\n=== Session Memory (previous compaction summary) ===\n"
            f"{content}\n=== End Session Memory ==="
        )

    # ── Layer 5: Progress + Features ──
    progress_text = _format_progress(session)
    if progress_text:
        content, _ = _truncate_to_budget(progress_text, LAYER_BUDGETS[5])
        layers.append(f"\n=== Progress ===\n{content}\n=== End Progress ===")

    # ── Layer 6: Agent Self-Knowledge ──
    experience_text = _load_experience(session.forge_dir)
    if experience_text:
        content, truncated = _truncate_to_budget(experience_text, LAYER_BUDGETS[6])
        if truncated:
            content += "\n[经验内容过长，已截断。]"
        layers.append(
            f"\n=== Agent Self-Knowledge (lessons from past sessions) ===\n"
            f"{content}\n=== End Agent Self-Knowledge ==="
        )

    # ── 总量检查：超 TOTAL_BUDGET 时从 Layer 4 开始逐层截断 ──
    result = "\n".join(layers)
    estimated_tokens = len(result) // 4
    if estimated_tokens > TOTAL_BUDGET:
        # 优先丢弃 Layer 5（Progress）和 Layer 4（Session Memory）
        # Layer 6（Self-Knowledge）比 Session Memory 更有价值，保留
        layers_to_keep = layers[:4] + ([layers[6]] if len(layers) > 6 else [])
        result = "\n".join(l for l in layers_to_keep if l)
        logger.warning(
            "System prompt exceeded budget (%d tokens), dropped layers 4-5",
            estimated_tokens,
        )

    return result


def _load_experience(forge_dir: str) -> str:
    """从 .forge/experience/ 读取经验内容，供 Layer 6 注入。"""
    try:
        from ..observability.experience import load_experience_for_prompt
        return load_experience_for_prompt(forge_dir)
    except Exception as e:
        logger.debug("Failed to load experience: %s", e)
        return ""


def _format_progress(session: Session) -> str:
    """将 progress_data 和 features_data 格式化为紧凑文本。"""
    parts: list[str] = []

    if session.progress_data:
        pd = session.progress_data
        parts.append(f"Current feature: {pd.get('current_feature', 'N/A')}")
        files = pd.get("files_modified", [])
        if files:
            parts.append(f"Files modified last session: {', '.join(files[:10])}")
        next_steps = pd.get("next_steps", "")
        if next_steps:
            parts.append(f"Next steps: {next_steps}")
        tests = pd.get("tests_status")
        if tests:
            parts.append(f"Tests: {json.dumps(tests, ensure_ascii=False)}")
        issues = pd.get("open_issues", [])
        if issues:
            parts.append(f"Open issues: {'; '.join(str(i) for i in issues[:5])}")

    if session.features_data:
        features = session.features_data.get("features", [])
        pending = [f for f in features if f.get("status") != "done"]
        if pending:
            parts.append("\nPending features:")
            for f in pending[:10]:
                status = f.get("status", "pending")
                parts.append(f"  [{status}] {f.get('id')}: {f.get('title', '')}")

    return "\n".join(parts)
