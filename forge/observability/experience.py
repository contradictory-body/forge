"""
experience.py — Agent 自我经验系统

职责：
1. 从已完成的 trace JSONL 文件提炼"经验教训"（extract_experience）
2. 当经验文件总量超过阈值时合并（merge_experiences）
3. 为 System Prompt 提供经验内容（load_experience_for_prompt）
4. 启动时统一调度以上任务（startup_experience_tasks）

文件布局：
  .forge/
  ├── traces/
  │   ├── {session_id}.jsonl        原始 trace
  │   └── {session_id}.done         提炼完成标记（sidecar 文件）
  └── experience/
      ├── exp_{timestamp}.md        单次会话提炼的经验（merged: false）
      └── exp_{timestamp}.md        合并后的经验（merged: true）

设计原则：
- 全部 fail-open：LLM 失败或文件 IO 失败都不影响 agent 正常运行
- 提炼和合并均为异步，不阻塞会话启动
- 触发条件明确：有 evaluator_verdict 事件才提炼；总字符超阈值才合并
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .tracer import iter_events, list_session_traces, TRACE_DIR_NAME

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.observability.experience")

EXPERIENCE_DIR_NAME = "experience"
EXPERIENCE_MERGE_THRESHOLD = 20000   # 总字符数超过此值触发合并
MIN_EVALUATIONS_TO_EXTRACT = 1       # 至少需要 N 条评审事件才提炼


# ── 提炼 Prompt ──────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = (
    "你是一个 coding agent 的自我改进助手。"
    "你的任务是从会话追踪数据中提炼出对 agent 未来行为有直接指导价值的经验教训。"
)

_EXTRACT_USER = """以下是某次 coding agent 会话的关键追踪事件（JSON 格式）。

{events_text}

请根据以上数据，提炼出 agent 在未来会话中应当注意的经验教训。

要求：
- 只提炼有数据支撑的经验，不要凭空推断
- 每条经验必须是具体可执行的建议（"做什么"或"避免什么"），而不是描述发生了什么
- 重点关注：评审失败的原因、工具使用的低效模式、违规频发的代码区域
- 控制在 8 条以内
- 如果数据不足以提炼有价值的经验，只输出"（本次会话无可提炼的经验）"

直接输出 Markdown 列表，不要有任何前言或解释：
- 经验1
- 经验2
...
"""


# ── 合并 Prompt ──────────────────────────────────────────────────────────

_MERGE_SYSTEM = (
    "你是一个 coding agent 的自我改进助手。"
    "你的任务是将多次会话积累的经验教训合并为一份简洁、高价值的总结。"
)

_MERGE_USER = """以下是多次会话提炼的经验教训文件，按时间从早到晚排列（越靠后越新）。

{experiences_text}

请将以上内容合并为一份经验总结，要求：
- 近期的经验（靠后的文件）保留更多细节
- 早期的经验只保留在多个文件中反复出现的规律
- 区分两类并分别列出：
  1. "高可信规律"：在 2 次或以上文件中都出现的经验
  2. "近期观察"：只在最近 1-2 次文件中出现的经验
- 总字数控制在 3000 字以内
- 每条经验依然是具体可执行的建议

输出格式：
## 高可信规律
- ...

## 近期观察
- ...
"""


# ── 主入口：启动时调度 ────────────────────────────────────────────────────

async def startup_experience_tasks(session: Session) -> None:
    """
    会话启动时异步执行的经验系统任务。
    由 init_observability_layer 通过 asyncio.ensure_future 调用，不阻塞启动。

    步骤：
    1. 找出所有已完成但未提炼的 trace 文件 → 逐个提炼
    2. 检查经验文件总量 → 必要时合并
    """
    try:
        await _extract_pending_traces(session)
    except Exception as e:
        logger.debug("Experience extraction failed: %s", e)

    try:
        await _check_and_merge(session)
    except Exception as e:
        logger.debug("Experience merge check failed: %s", e)


# ── 提炼 ─────────────────────────────────────────────────────────────────

async def _extract_pending_traces(session: Session) -> None:
    """找出所有已完成但未提炼的 trace，逐个提炼。"""
    trace_dir = Path(session.forge_dir) / TRACE_DIR_NAME
    if not trace_dir.exists():
        return

    pending = _find_pending_traces(trace_dir)
    if not pending:
        return

    logger.info("Found %d trace(s) pending experience extraction", len(pending))
    for trace_file in pending:
        try:
            await extract_experience(trace_file, session)
        except Exception as e:
            logger.debug("Failed to extract experience from %s: %s", trace_file.name, e)


def _find_pending_traces(trace_dir: Path) -> list[Path]:
    """
    找出所有满足提炼条件的 trace 文件：
    - 主文件（不含轮换后缀）
    - 包含 session_end 事件（会话完整结束）
    - 不存在对应的 .done sidecar 文件
    - 包含至少 MIN_EVALUATIONS_TO_EXTRACT 条 evaluator_verdict 事件
    """
    result = []
    for f in trace_dir.glob("*.jsonl"):
        # 只处理主文件
        if len(f.stem.split(".")) > 1:
            continue
        # 已提炼过
        done_marker = f.with_suffix(".done")
        if done_marker.exists():
            continue
        # 检查事件内容
        has_session_end = False
        eval_count = 0
        for ev in iter_events(f):
            t = ev.get("type")
            if t == "session_end":
                has_session_end = True
            elif t == "evaluator_verdict":
                eval_count += 1
        if has_session_end and eval_count >= MIN_EVALUATIONS_TO_EXTRACT:
            result.append(f)
    return result


async def extract_experience(trace_file: Path, session: Session) -> None:
    """
    从单个 trace 文件提炼经验，写入 experience/ 目录，
    并创建 .done sidecar 标记避免重复处理。
    """
    # 收集关键事件（只保留评审和工具调用，控制 token 消耗）
    events_text = _serialize_key_events(trace_file)
    if not events_text:
        _mark_done(trace_file)
        return

    prompt = _EXTRACT_USER.format(events_text=events_text)

    try:
        from ..core.query import query_llm
        response = await query_llm(
            model=session.aux_model,
            api_key=session.api_key,
            system_prompt=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tool_definitions=[],
            max_output_tokens=1000,
        )
        content = response.text.strip()
    except Exception as e:
        logger.warning("LLM call for experience extraction failed: %s", e)
        _mark_done(trace_file)  # 即使失败也标记，避免重复尝试同一个文件
        return

    # 跳过无价值内容
    if not content or "无可提炼" in content:
        logger.debug("No experience extracted from %s", trace_file.name)
        _mark_done(trace_file)
        return

    # 写经验文件
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    exp_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exp_file = exp_dir / f"exp_{timestamp}.md"

    # 统计元数据
    eval_count = sum(
        1 for ev in iter_events(trace_file)
        if ev.get("type") == "evaluator_verdict"
    )

    full_content = (
        f"---\n"
        f"source_session: {trace_file.stem}\n"
        f"evaluations: {eval_count}\n"
        f"period: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"merged: false\n"
        f"---\n\n"
        f"## 经验教训\n\n"
        f"{content}\n"
    )

    tmp = exp_file.with_suffix(".md.tmp")
    try:
        tmp.write_text(full_content, encoding="utf-8")
        tmp.replace(exp_file)
        logger.info("Experience extracted → %s", exp_file.name)
    except Exception as e:
        logger.warning("Failed to write experience file: %s", e)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    finally:
        _mark_done(trace_file)


def _mark_done(trace_file: Path) -> None:
    """创建 .done sidecar 文件标记该 trace 已处理。"""
    try:
        trace_file.with_suffix(".done").touch()
    except Exception:
        pass


def _serialize_key_events(trace_file: Path) -> str:
    """
    从 trace 文件提取对经验提炼有价值的事件，序列化为紧凑文本。
    只保留 evaluator_verdict 和 tool_call（错误类），控制总长度。
    """
    parts: list[str] = []
    total_chars = 0
    MAX_CHARS = 4000  # 控制 LLM 输入大小

    for ev in iter_events(trace_file):
        t = ev.get("type")
        d = ev.get("data", {})

        if t == "evaluator_verdict":
            line = json.dumps({
                "type": t,
                "verdict": d.get("verdict"),
                "score": d.get("score"),
                "criteria": d.get("criteria_results", []),
            }, ensure_ascii=False)
        elif t == "tool_call" and d.get("is_error"):
            line = json.dumps({
                "type": t,
                "tool": d.get("tool"),
                "is_error": True,
                "permission": d.get("permission"),
            }, ensure_ascii=False)
        else:
            continue

        if total_chars + len(line) > MAX_CHARS:
            break
        parts.append(line)
        total_chars += len(line)

    return "\n".join(parts)


# ── 合并 ─────────────────────────────────────────────────────────────────

async def _check_and_merge(session: Session) -> None:
    """检查经验文件总量，超过阈值则触发合并。"""
    exp_dir = Path(session.forge_dir) / EXPERIENCE_DIR_NAME
    if not exp_dir.exists():
        return

    exp_files = sorted(exp_dir.glob("exp_*.md"), key=lambda p: p.stat().st_mtime)
    if len(exp_files) < 2:
        return

    total_chars = sum(f.stat().st_size for f in exp_files)
    if total_chars <= EXPERIENCE_MERGE_THRESHOLD:
        return

    logger.info(
        "Experience total chars %d > threshold %d, triggering merge",
        total_chars, EXPERIENCE_MERGE_THRESHOLD,
    )
    await merge_experiences(exp_dir, exp_files, session)


async def merge_experiences(
    exp_dir: Path,
    exp_files: list[Path],
    session: Session,
) -> None:
    """
    将所有经验文件合并为一个新文件，删除旧文件。
    时序信息通过文件排列顺序（按 mtime 从早到晚）传递给 LLM。
    """
    # 按时间从早到晚拼接
    parts: list[str] = []
    for i, f in enumerate(exp_files):
        try:
            content = f.read_text(encoding="utf-8")
            parts.append(f"=== 文件 {i+1}（{'较早' if i < len(exp_files)//2 else '较新'}）===\n{content}")
        except Exception:
            continue

    if not parts:
        return

    experiences_text = "\n\n".join(parts)
    prompt = _MERGE_USER.format(experiences_text=experiences_text[:8000])

    try:
        from ..core.query import query_llm
        response = await query_llm(
            model=session.aux_model,
            api_key=session.api_key,
            system_prompt=_MERGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tool_definitions=[],
            max_output_tokens=2000,
        )
        merged_content = response.text.strip()
    except Exception as e:
        logger.warning("LLM merge call failed: %s", e)
        return

    if not merged_content:
        return

    # 写合并文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    merged_file = exp_dir / f"exp_{timestamp}.md"

    full_content = (
        f"---\n"
        f"merged: true\n"
        f"source_files: {len(exp_files)}\n"
        f"period: {exp_files[0].stem} ~ {exp_files[-1].stem}\n"
        f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"---\n\n"
        f"{merged_content}\n"
    )

    tmp = merged_file.with_suffix(".md.tmp")
    try:
        tmp.write_text(full_content, encoding="utf-8")
        tmp.replace(merged_file)
        # 删除被合并的旧文件
        for f in exp_files:
            try:
                f.unlink()
            except Exception:
                pass
        logger.info(
            "Merged %d experience files → %s", len(exp_files), merged_file.name
        )
    except Exception as e:
        logger.warning("Failed to write merged experience file: %s", e)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ── 为 System Prompt 提供经验内容 ────────────────────────────────────────

def load_experience_for_prompt(forge_dir: str) -> str:
    """
    读取所有经验文件，按时间倒序拼接（最新的在前），返回供注入 prompt 的文本。
    供 assembler.py 的 Layer 6 调用，本函数是同步的。
    """
    exp_dir = Path(forge_dir) / EXPERIENCE_DIR_NAME
    if not exp_dir.exists():
        return ""

    exp_files = sorted(
        exp_dir.glob("exp_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # 最新的在前
    )
    if not exp_files:
        return ""

    parts: list[str] = []
    for f in exp_files:
        try:
            content = f.read_text(encoding="utf-8")
            # 去掉 YAML front matter，只保留正文
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    content = content[end + 3:].strip()
            parts.append(content)
        except Exception:
            continue

    return "\n\n---\n\n".join(parts)


# ── 熵管理（供 entropy.py 调用）─────────────────────────────────────────

def cleanup_expired_traces(forge_dir: str, retention_days: int = 7) -> None:
    """
    删除超过 retention_days 天的 trace 文件。

    删除前确认：
    - 已有 .done sidecar（已提炼过），或
    - 文件中没有 evaluator_verdict 事件（本就不需要提炼）
    不满足以上条件的文件即使过期也暂时保留，等下次启动时提炼后再删。
    """
    from datetime import timedelta
    trace_dir = Path(forge_dir) / TRACE_DIR_NAME
    if not trace_dir.exists():
        return

    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0

    for f in trace_dir.glob("*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime >= cutoff:
                continue
            # 检查是否可以安全删除
            done_marker = f.with_suffix(".done")
            if done_marker.exists():
                f.unlink()
                done_marker.unlink(missing_ok=True)
                deleted += 1
            else:
                # 没有 .done 标记，检查是否有评审事件
                has_eval = any(
                    ev.get("type") == "evaluator_verdict"
                    for ev in iter_events(f)
                )
                if not has_eval:
                    # 无需提炼，直接删除
                    f.unlink()
                    deleted += 1
                else:
                    # 有评审事件但未提炼，暂时保留
                    logger.debug(
                        "Trace %s is expired but not extracted, keeping for now",
                        f.name,
                    )
        except Exception as e:
            logger.debug("Error processing trace %s: %s", f.name, e)

    if deleted:
        logger.info("Trace GC: deleted %d expired trace files", deleted)
