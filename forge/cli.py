"""
cli.py — Forge CLI 入口

使用方式：
    forge                    # 在当前目录启动 Execute Mode
    forge --plan             # 启动 Plan Mode（只读）
    forge --model <model>    # 指定代码编写模型
    forge --aux-model <model> # 指定辅助模型（摘要/评审等）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .core import Session, SessionMode
from .core.loop import run_loop
from .core.tool_dispatch import ToolDispatch
from .core.permission import load_permission_rules
from .util.terminal_ui import (
    print_banner, print_error, print_info, print_assistant,
)

logger = logging.getLogger("forge.cli")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge — Terminal Coding Agent with Harness Engineering",
    )
    parser.add_argument(
        "--plan", action="store_true",
        help="Start in Plan Mode (read-only tools only)",
    )
    parser.add_argument(
        "--model", default="qwen3-coder-plus",
        help="Code writing model for the main agentic loop (default: qwen3-coder-plus)",
    )
    parser.add_argument(
        "--api-key", default=None,
        help="Anthropic API key (default: $ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=200,
        help="Max iterations per conversation turn (default: 200)",
    )
    parser.add_argument(
        "--aux-model", default="qwen3.5-plus",
        help="Auxiliary model for summarization, evaluation, etc. (default: qwen3.5-plus)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


async def create_session(args: argparse.Namespace) -> Session:
    """根据命令行参数创建 Session 对象。"""
    project_root = str(Path.cwd().resolve())
    forge_dir = str(Path(project_root) / ".forge")
    Path(forge_dir).mkdir(parents=True, exist_ok=True)

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    session = Session(
        project_root=project_root,
        forge_dir=forge_dir,
        mode=SessionMode.PLAN if args.plan else SessionMode.EXECUTE,
        model=args.model,
        aux_model=args.aux_model,
        api_key=api_key,
        max_iterations=args.max_iterations,
    )

    # 4. 加载权限规则（先读 FORGE.md 内容供解析）
    forge_md_path = Path(project_root) / "FORGE.md"
    if forge_md_path.exists():
        try:
            session.forge_md_content = forge_md_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read FORGE.md: %s", e)

    # ── FORGE.md 自然语言编译（缓存优先）──
    from .constraints.forge_md_compiler import (
        try_load_cache_only, compile_forge_md, format_compiled_preview,
    )
    compiled = try_load_cache_only(session.forge_md_content, forge_dir)
    if compiled is None and session.forge_md_content and api_key:
        try:
            import asyncio as _asyncio
            compiled = await _asyncio.wait_for(
                compile_forge_md(
                    session.forge_md_content, forge_dir, args.aux_model, api_key,
                ),
                timeout=1800.0,
            )
            from .util.terminal_ui import print_info
            print_info("Parsed FORGE.md:\n" + format_compiled_preview(compiled))
        except _asyncio.TimeoutError:
            logger.warning("FORGE.md compile timed out (1800s), using legacy parser")
            compiled = None
        except Exception as e:
            logger.warning("FORGE.md compile failed: %s", e)
            compiled = None
    session.forge_md_compiled = compiled
    session.permission_rules = (
        compiled.to_permission_rules()
        if compiled is not None
        else load_permission_rules(session.forge_md_content)
    )

    # 5. 注册内置工具
    ToolDispatch.register_builtin_tools(session)

    # 7. 读取 auto memory
    memory_dir = Path(forge_dir) / "memory"
    if memory_dir.exists():
        md_files = sorted(
            memory_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        parts: list[str] = []
        total_len = 0
        for f in md_files:
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if total_len + len(text) > 25000:
                break
            parts.append(f"--- {f.name} ---\n{text}")
            total_len += len(text)
        session.auto_memory_content = "\n\n".join(parts)

    # 8. 读取 progress.json
    progress_path = Path(forge_dir) / "progress.json"
    if progress_path.exists():
        try:
            session.progress_data = json.loads(
                progress_path.read_text(encoding="utf-8")
            )
        except Exception as e:
            logger.warning("Failed to read progress.json: %s", e)

    # 9. 读取 features.json
    features_path = Path(forge_dir) / "features.json"
    if features_path.exists():
        try:
            session.features_data = json.loads(
                features_path.read_text(encoding="utf-8")
            )
        except Exception as e:
            logger.warning("Failed to read features.json: %s", e)

    # ── Dev B / Dev C 集成点 ──
    # init_context_layer(session)   # Dev B 注册
    # init_quality_layer(session)   # Dev C 注册
    try:
        from .context import init_context_layer
        init_context_layer(session)
        logger.debug("Context layer initialized")
    except ImportError:
        logger.debug("Context layer not available (Dev B not integrated)")

    try:
        from .constraints import init_quality_layer
        init_quality_layer(session)
        logger.debug("Quality layer initialized")
    except ImportError:
        logger.debug("Quality layer not available (Dev C not integrated)")

    # ── Observability 层（最后初始化以便包装前面层注册的回调）──
    try:
        from .observability import init_observability_layer
        init_observability_layer(session)
        logger.debug("Observability layer initialized")
    except ImportError:
        logger.debug("Observability layer not available")
    except Exception as e:
        logger.warning("Observability init failed (non-fatal): %s", e)

    return session


async def _run_session_end_hooks(session: Session) -> None:
    """调用所有 on_session_end 钩子。"""
    for hook in session.hooks.get("on_session_end", []):
        try:
            await hook(session)
        except Exception as e:
            logger.warning("on_session_end hook error: %s", e)


def _handle_command(session: Session, cmd: str) -> str | None:
    """
    处理斜杠命令。返回 None 表示继续 REPL，返回 "exit" 表示退出。
    """
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command in ("/exit", "/quit"):
        return "exit"

    elif command == "/mode":
        if arg == "plan":
            session.mode = SessionMode.PLAN
            print_info("Switched to Plan Mode (read-only)")
        elif arg == "execute":
            session.mode = SessionMode.EXECUTE
            print_info("Switched to Execute Mode")
        else:
            print_info("Usage: /mode plan | /mode execute")

    elif command == "/compact":
        if session.compact_callback is not None:
            asyncio.get_event_loop().run_until_complete(
                session.compact_callback(session)
            )
            print_info("Compaction completed")
        else:
            print_info("Compaction not enabled (context layer not loaded)")

    elif command == "/status":
        ratio = session.token_usage_ratio
        print_info(
            f"  Mode       : {session.mode.value}\n"
            f"  Code model : {session.model}\n"
            f"  Aux model  : {session.aux_model}\n"
            f"  Tools      : {len(session.tool_registry)}\n"
            f"  Iterations : {session.iteration_count}\n"
            f"  Tokens in  : {session.total_input_tokens:,}\n"
            f"  Tokens out : {session.total_output_tokens:,}\n"
            f"  Context    : {ratio:.1%} used\n"
            f"  Messages   : {len(session.messages)}\n"
            f"  Compactions: {session.compaction_count}"
        )

    elif command == "/tools":
        print_info("Registered tools:")
        for name, td in sorted(session.tool_registry.items()):
            flag = "R" if td.readonly else "W"
            print_info(f"  [{flag}] {name:25s} — {td.description[:60]}")

    elif command == "/undo":
        from .tools.git import git_restore_snapshot
        result = asyncio.get_event_loop().run_until_complete(
            git_restore_snapshot(session)
        )
        print_info(result)

    elif command == "/trace":
        try:
            from .observability.analyzer import cmd_trace
            print_info(cmd_trace(session, arg))
        except Exception as e:
            print_info(f"/trace failed: {e}")

    elif command == "/trend":
        try:
            from .observability.analyzer import cmd_trend
            print_info(cmd_trend(session, arg))
        except Exception as e:
            print_info(f"/trend failed: {e}")

    elif command == "/help":
        print_info(
            "Commands:\n"
            "  /exit, /quit     Exit Forge\n"
            "  /mode plan|execute  Switch mode\n"
            "  /compact         Trigger context compaction\n"
            "  /status          Show session status\n"
            "  /tools           List registered tools\n"
            "  /undo            Revert last file edit\n"
            "  /help            Show this help"
        )

    else:
        print_info(f"Unknown command: {command}. Type /help for available commands.")

    return None


async def main() -> None:
    """主函数：解析参数 → 创建 Session → REPL 循环。"""
    args = parse_args()

    # 配置日志
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    session = await create_session(args)

    if not session.api_key:
        print_error(
            "No API key found. Set ANTHROPIC_API_KEY environment variable "
            "or pass --api-key."
        )
        sys.exit(1)

    print_banner(
        model=session.model,
        mode=session.mode.value,
        tool_count=len(session.tool_registry),
    )
    print_info("Type your request, or /help for commands.\n")

    try:
        while True:
            # 读取用户输入
            # Ctrl+C 在此处 = 用户想退出 Forge
            try:
                user_input = input("You ▸ ").strip()
            except (EOFError, KeyboardInterrupt):
                print_info("\n（按 Ctrl+C 退出）")
                break

            if not user_input:
                continue

            # 斜杠命令
            if user_input.startswith("/"):
                result = _handle_command(session, user_input)
                if result == "exit":
                    break
                continue

            # Agentic loop
            # Ctrl+C 在此处 = 中断当前任务，返回命令行（不退出 Forge）
            try:
                await run_loop(session, user_input)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print_info(
                    "\n⚡ 已中断当前任务。"
                    "\n   输入新指令继续工作，或输入 /exit 退出。"
                )
            except Exception as e:
                logger.exception("Error in agentic loop")
                print_error(f"Error: {type(e).__name__}: {e}")

    except KeyboardInterrupt:
        # 只有在最外层捕获到才真正退出
        pass

    # 退出清理
    print_info("\nShutting down...")
    await _run_session_end_hooks(session)
    print_info("Goodbye.")


def entry() -> None:
    """setuptools 入口点。"""
    asyncio.run(main())


if __name__ == "__main__":
    entry()
