"""
compiler_integration.py — FORGE.md 编译器集成补丁（修正版）

修正前一版的 bug：旧版的 compile_forge_md_sync 内部用了 asyncio.run()，
而 create_session 是在 main() 的事件循环里被调用的，会报
RuntimeError: asyncio.run() cannot be called from a running event loop。

正确做法：把 create_session 改成 async，从 main() 里 await 它。

改动路径：
1. cli.py — create_session 变为 async，main() 中 session = await create_session(args)
2. cli.py — 在 await 之前走 try_load_cache_only 做同步 fast-path，
             缓存未命中时才 await 真正的 compile_forge_md
3. constraints/__init__.py — 优先复用 session.forge_md_compiled

为什么保留"先 cache fast-path，再 async compile"的两段式：
- 绝大多数情况 FORGE.md 没改动，缓存命中，零 await 延迟
- 缓存未命中时才付出 async 成本
"""

# ─────────────────────────────────────────────────────────────────────────
# Marker-based 幂等 patch（可由安装脚本 / CI 调用）
# ─────────────────────────────────────────────────────────────────────────

CLI_DEF_OLD = "def create_session(args: argparse.Namespace) -> Session:"
CLI_DEF_NEW = "async def create_session(args: argparse.Namespace) -> Session:"

CLI_MAIN_OLD = "    session = create_session(args)"
CLI_MAIN_NEW = "    session = await create_session(args)"

CLI_PERM_OLD = (
    "    session.permission_rules = load_permission_rules(session.forge_md_content)"
)

CLI_PERM_NEW = """    # ── FORGE.md 自然语言编译（缓存优先）──
    from .constraints.forge_md_compiler import (
        try_load_cache_only, compile_forge_md, format_compiled_preview,
    )
    compiled = try_load_cache_only(session.forge_md_content, forge_dir)
    if compiled is None and session.forge_md_content and api_key:
        try:
            compiled = await compile_forge_md(
                session.forge_md_content, forge_dir, args.model, api_key,
            )
            from .util.terminal_ui import print_info
            print_info("Parsed FORGE.md:\\n" + format_compiled_preview(compiled))
        except Exception as e:
            logger.warning("FORGE.md compile failed: %s", e)
            compiled = None
    session.forge_md_compiled = compiled
    session.permission_rules = (
        compiled.to_permission_rules()
        if compiled is not None
        else load_permission_rules(session.forge_md_content)
    )"""


CONSTRAINTS_OLD = """    # 1. 解析约束规则
    config = parse_constraints(session.forge_md_content)
    set_config(config)"""

CONSTRAINTS_NEW = """    # 1. 解析约束规则（优先使用已编译的结果）
    compiled = getattr(session, "forge_md_compiled", None)
    if compiled is not None:
        config = compiled.to_constraint_config()
    else:
        config = parse_constraints(session.forge_md_content)
    set_config(config)"""


def apply_patches(forge_root: str) -> dict[str, bool]:
    """幂等应用补丁。返回每个文件的 patched 状态。"""
    from pathlib import Path
    results: dict[str, bool] = {}

    # cli.py - 三处替换必须全部命中
    cli_path = Path(forge_root) / "cli.py"
    if cli_path.exists():
        src = cli_path.read_text(encoding="utf-8")
        if "try_load_cache_only" in src:
            results["cli.py"] = False  # already patched
        else:
            patched_count = 0
            if CLI_DEF_OLD in src:
                src = src.replace(CLI_DEF_OLD, CLI_DEF_NEW, 1)
                patched_count += 1
            if CLI_MAIN_OLD in src:
                src = src.replace(CLI_MAIN_OLD, CLI_MAIN_NEW, 1)
                patched_count += 1
            if CLI_PERM_OLD in src:
                src = src.replace(CLI_PERM_OLD, CLI_PERM_NEW, 1)
                patched_count += 1
            if patched_count == 3:
                cli_path.write_text(src, encoding="utf-8")
                results["cli.py"] = True
            else:
                results["cli.py"] = False  # partial match, abort
    else:
        results["cli.py"] = False

    # constraints/__init__.py
    c_path = Path(forge_root) / "constraints" / "__init__.py"
    if c_path.exists():
        src = c_path.read_text(encoding="utf-8")
        if "forge_md_compiled" in src:
            results["constraints/__init__.py"] = False
        elif CONSTRAINTS_OLD in src:
            c_path.write_text(
                src.replace(CONSTRAINTS_OLD, CONSTRAINTS_NEW, 1),
                encoding="utf-8",
            )
            results["constraints/__init__.py"] = True
        else:
            results["constraints/__init__.py"] = False
    else:
        results["constraints/__init__.py"] = False

    return results


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "forge"
    results = apply_patches(target)
    for f, ok in results.items():
        print(f"{'✓' if ok else '·'} {f}")
