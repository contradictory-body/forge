"""
analyzer.py — Trace 分析与漂移检测

提供三类功能：
1. summarize_session(trace_file) — 单次会话的执行画像
2. criterion_trend(forge_dir, criterion_name, window) — 某个评审维度的滑动均值
3. startup_drift_check(session) — 启动时扫最近 N 次会话，发现长期下滑

给 /trace、/trend、/drift 三个 REPL 命令提供支持。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import TYPE_CHECKING

from .tracer import TRACE_DIR_NAME, iter_events, list_session_traces

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.observability.analyzer")

# 漂移阈值：评审得分近 5 次均值比之前低于 15 分就警告
DRIFT_WINDOW = 5
DRIFT_DELTA = 15
DRIFT_LOOKBACK_SESSIONS = 10


# ── 会话画像 ────────────────────────────────────────────────────────────

def summarize_session(trace_file: Path) -> dict:
    """
    读一个 trace 文件，返回结构化统计。用于 /trace 命令。
    """
    stats: dict = {
        "session_id": trace_file.stem,
        "turns": 0,
        "llm_calls": 0,
        "llm_total_duration_ms": 0,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "tool_calls_by_name": defaultdict(int),
        "tool_calls_duration_by_name": defaultdict(int),
        "tool_errors": 0,
        "permission_decisions": defaultdict(int),
        "compactions": [],
        "evaluator_verdicts": [],
        "errors": [],
    }

    for ev in iter_events(trace_file):
        t = ev.get("type")
        d = ev.get("data") or {}

        if t == "turn_start":
            stats["turns"] += 1
        elif t == "llm_call":
            stats["llm_calls"] += 1
            stats["llm_total_duration_ms"] += d.get("duration_ms", 0)
            stats["llm_input_tokens"] += d.get("input_tokens", 0)
            stats["llm_output_tokens"] += d.get("output_tokens", 0)
        elif t == "llm_call_error":
            stats["errors"].append({"kind": "llm", **d})
        elif t == "tool_call":
            name = d.get("tool", "?")
            stats["tool_calls_by_name"][name] += 1
            stats["tool_calls_duration_by_name"][name] += d.get("duration_ms", 0)
            if d.get("is_error"):
                stats["tool_errors"] += 1
            perm = d.get("permission", "unknown")
            stats["permission_decisions"][perm] += 1
        elif t == "compaction":
            stats["compactions"].append({
                "duration_ms": d.get("duration_ms"),
                "ratio_before": d.get("ratio_before"),
                "ratio_after": d.get("ratio_after"),
            })
        elif t == "evaluator_verdict":
            stats["evaluator_verdicts"].append({
                "verdict": d.get("verdict"),
                "score": d.get("score"),
                "criteria": d.get("criteria_results", []),
            })

    # 转普通 dict 方便 JSON 序列化
    stats["tool_calls_by_name"] = dict(stats["tool_calls_by_name"])
    stats["tool_calls_duration_by_name"] = dict(stats["tool_calls_duration_by_name"])
    stats["permission_decisions"] = dict(stats["permission_decisions"])
    return stats


def format_summary(stats: dict) -> str:
    """格式化为人类可读的摘要（给 /trace 命令用）。"""
    lines = [
        f"Session : {stats['session_id']}",
        f"Turns   : {stats['turns']}",
        f"LLM     : {stats['llm_calls']} calls, "
        f"{stats['llm_total_duration_ms']} ms total, "
        f"{stats['llm_input_tokens']:,} in / "
        f"{stats['llm_output_tokens']:,} out tokens",
    ]
    if stats["tool_calls_by_name"]:
        lines.append("Tools:")
        items = sorted(
            stats["tool_calls_by_name"].items(),
            key=lambda kv: -kv[1],
        )
        for name, count in items:
            dur = stats["tool_calls_duration_by_name"].get(name, 0)
            lines.append(f"  {name:22s} × {count:4d}  ({dur} ms total)")

    if stats["tool_errors"]:
        lines.append(f"Tool errors: {stats['tool_errors']}")
    if stats["permission_decisions"]:
        lines.append(f"Permissions: {stats['permission_decisions']}")
    if stats["compactions"]:
        lines.append(f"Compactions: {len(stats['compactions'])}")
        for c in stats["compactions"]:
            lines.append(
                f"  {c['ratio_before']} → {c['ratio_after']} "
                f"({c['duration_ms']} ms)"
            )
    if stats["evaluator_verdicts"]:
        lines.append(f"Evaluations: {len(stats['evaluator_verdicts'])}")
        for v in stats["evaluator_verdicts"]:
            lines.append(f"  {v['verdict']} — score {v['score']}")
    if stats["errors"]:
        lines.append(f"Errors: {len(stats['errors'])}")
    return "\n".join(lines)


# ── Criterion 趋势分析 ───────────────────────────────────────────────────

def criterion_trend(
    forge_dir: str,
    criterion_name: str | None = None,
    window: int = DRIFT_WINDOW,
) -> dict:
    """
    分析 evaluator_verdict 事件在最近 N 次会话中的趋势。

    如果 criterion_name 为 None，返回所有 criteria 的趋势；
    否则只返回指定 criterion。

    返回结构：
      {
        criterion_name: {
          "recent_mean": float,        # 最近 window 次的平均分
          "prior_mean": float,         # 之前 window 次的平均分
          "delta": float,              # recent - prior
          "is_drifting": bool,         # |delta| >= DRIFT_DELTA 且 delta < 0
          "sample_count": int,
        }
      }
    """
    # 收集所有 trace 文件（最近 30 个会话足够）
    trace_files = list_session_traces(forge_dir)[:30]

    # 按时间正序排列（最早的在前）以便做时序趋势
    trace_files = list(reversed(trace_files))

    # 收集评审记录：按 criterion 名分组
    scores_by_criterion: dict[str, list[float]] = defaultdict(list)
    for tf in trace_files:
        for ev in iter_events(tf):
            if ev.get("type") != "evaluator_verdict":
                continue
            d = ev.get("data", {})
            for c in d.get("criteria_results", []) or []:
                if not isinstance(c, dict):
                    continue
                cname = c.get("name")
                score = c.get("score")
                if cname and isinstance(score, (int, float)):
                    scores_by_criterion[cname].append(float(score))

    result: dict = {}
    for cname, scores in scores_by_criterion.items():
        if criterion_name and cname != criterion_name:
            continue
        if len(scores) < 2:
            continue

        recent = scores[-window:]
        prior = scores[-(2 * window):-window] if len(scores) >= 2 * window else scores[:-window]

        if not prior:
            continue

        recent_mean = mean(recent)
        prior_mean = mean(prior)
        delta = recent_mean - prior_mean

        result[cname] = {
            "recent_mean": round(recent_mean, 2),
            "prior_mean": round(prior_mean, 2),
            "delta": round(delta, 2),
            "is_drifting": delta <= -DRIFT_DELTA,
            "sample_count": len(scores),
        }
    return result


def format_trend(trend: dict) -> str:
    """格式化 criterion_trend 输出为可读文本。"""
    if not trend:
        return "(no evaluation data yet — run a few sessions first)"
    lines = ["Criterion           recent  prior  Δ      samples  drift?"]
    lines.append("─" * 60)
    for cname, info in sorted(trend.items()):
        marker = " ⚠ " if info["is_drifting"] else "   "
        lines.append(
            f"{cname:18s}  {info['recent_mean']:6.1f}  "
            f"{info['prior_mean']:5.1f}  "
            f"{info['delta']:+6.1f}  "
            f"{info['sample_count']:7d} {marker}"
        )
    return "\n".join(lines)


# ── 启动时漂移检测 ──────────────────────────────────────────────────────

def startup_drift_check(session: Session) -> None:
    """
    启动时运行一次漂移检测。
    有评审得分显著下降 → 打印警告到 stdout（非阻塞）。

    故意不打扰用户太多：只在确实有漂移时输出，且信息极简。
    """
    trend = criterion_trend(session.forge_dir)
    drifting = [(n, info) for n, info in trend.items() if info["is_drifting"]]
    if not drifting:
        return

    print("\n  ⚠  Quality drift detected:")
    for cname, info in drifting:
        print(
            f"     {cname}: recent {info['recent_mean']:.1f} "
            f"(was {info['prior_mean']:.1f}, "
            f"Δ {info['delta']:+.1f} over {info['sample_count']} samples)"
        )
    print("     Run /trend to see details.\n")


# ── 可用于 REPL 命令的便利包装 ──────────────────────────────────────────

def cmd_trace(session: Session, arg: str = "") -> str:
    """/trace [session_id] — 显示某次会话（默认当前）的 trace 摘要。"""
    traces = list_session_traces(session.forge_dir)
    if not traces:
        return "(no traces yet)"

    if arg:
        # 允许部分匹配 session_id
        matches = [t for t in traces if arg in t.stem]
        if not matches:
            return f"no trace matching '{arg}'"
        target = matches[0]
    else:
        # 默认当前会话（tracer 是最新的文件）
        tracer = getattr(session, "tracer", None)
        if tracer:
            target = Path(session.forge_dir) / TRACE_DIR_NAME / f"{tracer.session_id}.jsonl"
            if not target.exists():
                target = traces[0]
        else:
            target = traces[0]

    stats = summarize_session(target)
    return format_summary(stats)


def cmd_trend(session: Session, arg: str = "") -> str:
    """/trend [criterion] — 显示评审维度的长期趋势。"""
    trend = criterion_trend(session.forge_dir, criterion_name=arg or None)
    return format_trend(trend)
