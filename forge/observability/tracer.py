"""
tracer.py — JSONL 事件追踪器

每条事件一行 JSON，写入 .forge/traces/{session_id}.jsonl。
为什么选 JSONL：
- 追加写入原子性好（不需要锁）
- 可增量读取（不必读完整文件）
- 易于用 jq/grep 分析
- 与现有 .forge/ 目录风格一致

大小管理：
- 超过 10MB 自动轮换到 {session_id}.{N}.jsonl
- 保留最近 10 个会话的 trace，由 entropy.py 风格的清理复用
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("forge.observability.tracer")

TRACE_DIR_NAME = "traces"
ROTATE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
KEEP_SESSIONS = 10                     # 最近 N 个会话的 trace 文件


class Tracer:
    """
    单会话追踪器。每个 Forge 会话对应一个 Tracer 实例，写一个 JSONL 文件。

    所有事件字段：
        ts          : 事件时间戳（float, unix epoch）
        session_id  : 会话 UUID 前 12 位
        type        : 事件类型（str）
        data        : 事件负载（dict）
    """

    def __init__(self, forge_dir: str):
        self.session_id = uuid4().hex[:12]
        self.forge_dir = Path(forge_dir)
        self.trace_dir = self.forge_dir / TRACE_DIR_NAME
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._rotation_index = 0
        self._start_time = time.time()
        self._disabled = False

        # 启动时清理老 trace
        try:
            self._gc_old_traces()
        except Exception as e:
            logger.debug("Trace GC failed: %s", e)

    @property
    def trace_file(self) -> Path:
        """当前写入的 trace 文件路径。"""
        if self._rotation_index == 0:
            return self.trace_dir / f"{self.session_id}.jsonl"
        return self.trace_dir / f"{self.session_id}.{self._rotation_index}.jsonl"

    def emit(self, event_type: str, data: dict | None = None) -> None:
        """
        发射一条事件。失败时静默 disable 自己（不抛异常影响 agent）。
        """
        if self._disabled:
            return

        payload = {
            "ts": time.time(),
            "elapsed_s": round(time.time() - self._start_time, 3),
            "session_id": self.session_id,
            "type": event_type,
            "data": data or {},
        }
        try:
            path = self.trace_file
            # 写入前检查是否需要轮换
            if path.exists() and path.stat().st_size > ROTATE_SIZE_BYTES:
                self._rotation_index += 1
                path = self.trace_file

            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("Tracer emit failed, disabling: %s", e)
            self._disabled = True

    def emit_timed(self, event_type: str, duration_s: float, data: dict | None = None) -> None:
        """发射一条带耗时信息的事件。"""
        d = dict(data or {})
        d["duration_ms"] = int(duration_s * 1000)
        self.emit(event_type, d)

    def close(self) -> None:
        """写入 session_end 事件。"""
        if self._disabled:
            return
        self.emit("session_end", {
            "total_elapsed_s": round(time.time() - self._start_time, 3),
            "rotations": self._rotation_index,
        })

    def _gc_old_traces(self) -> None:
        """删除超过 KEEP_SESSIONS 个会话的 trace 文件（按 mtime 排序）。"""
        all_traces = sorted(
            self.trace_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # 按 session_id 分组 — 每个 session 可能有多个轮换文件
        seen_sessions: set[str] = set()
        to_delete: list[Path] = []
        for f in all_traces:
            # {session_id}.jsonl 或 {session_id}.{N}.jsonl
            sid = f.stem.split(".")[0]
            if sid in seen_sessions:
                # 同一 session 的轮换文件保留
                if len(seen_sessions) >= KEEP_SESSIONS:
                    to_delete.append(f)
                continue
            seen_sessions.add(sid)
            if len(seen_sessions) > KEEP_SESSIONS:
                to_delete.append(f)

        for f in to_delete:
            try:
                f.unlink()
            except Exception:
                pass
        if to_delete:
            logger.debug("Trace GC: removed %d old files", len(to_delete))


def iter_events(trace_file: Path):
    """流式读取 JSONL 事件（给 analyzer 用）。"""
    if not trace_file.exists():
        return
    with trace_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def list_session_traces(forge_dir: str) -> list[Path]:
    """列出所有会话的 trace 文件（主文件，不含轮换）。"""
    trace_dir = Path(forge_dir) / TRACE_DIR_NAME
    if not trace_dir.exists():
        return []
    # 只返回 {session_id}.jsonl，不含 {session_id}.{N}.jsonl
    result = []
    for f in trace_dir.glob("*.jsonl"):
        parts = f.stem.split(".")
        if len(parts) == 1:  # 主文件
            result.append(f)
    return sorted(result, key=lambda p: p.stat().st_mtime, reverse=True)
