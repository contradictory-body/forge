"""
cli_patch.py — Integration patch for forge/cli.py

两处修改：
1. create_session() 末尾增加 init_observability_layer 调用
2. _handle_command() 增加 /trace 和 /trend 两个斜杠命令

使用方式：手动 apply 下面的 diff 到 forge/cli.py，或查看本文件内的
`apply_patch()` 函数参考实现。
"""

# ─────────────────────────────────────────────────────────────────────────
# Patch #1 — 在 create_session() 末尾、在 context 和 quality 初始化之后
# ─────────────────────────────────────────────────────────────────────────
#
# 原代码：
#     try:
#         from .constraints import init_quality_layer
#         init_quality_layer(session)
#         logger.debug("Quality layer initialized")
#     except ImportError:
#         logger.debug("Quality layer not available (Dev C not integrated)")
#
#     return session
#
# 替换为：
#     try:
#         from .constraints import init_quality_layer
#         init_quality_layer(session)
#         logger.debug("Quality layer initialized")
#     except ImportError:
#         logger.debug("Quality layer not available (Dev C not integrated)")
#
#     # ── Observability 层（必须最后初始化以便包装前面层注册的回调）──
#     try:
#         from .observability import init_observability_layer
#         init_observability_layer(session)
#         logger.debug("Observability layer initialized")
#     except ImportError:
#         logger.debug("Observability layer not available")
#     except Exception as e:
#         logger.warning("Observability init failed (non-fatal): %s", e)
#
#     return session


# ─────────────────────────────────────────────────────────────────────────
# Patch #2 — 在 _handle_command() 新增 /trace 和 /trend 命令
# ─────────────────────────────────────────────────────────────────────────
#
# 在 `elif command == "/help":` 分支之前插入：
#
#     elif command == "/trace":
#         try:
#             from .observability.analyzer import cmd_trace
#             print_info(cmd_trace(session, arg))
#         except Exception as e:
#             print_info(f"/trace failed: {e}")
#
#     elif command == "/trend":
#         try:
#             from .observability.analyzer import cmd_trend
#             print_info(cmd_trend(session, arg))
#         except Exception as e:
#             print_info(f"/trend failed: {e}")
#
# 并在 /help 输出的命令列表中加入：
#     "  /trace [session]  Show execution trace summary\n"
#     "  /trend [criterion] Show evaluator score trend\n"


# ─────────────────────────────────────────────────────────────────────────
# 参考：以编程方式打补丁（供 CI/安装脚本复用）
# ─────────────────────────────────────────────────────────────────────────

PATCH_MARKER_1_OLD = """    try:
        from .constraints import init_quality_layer
        init_quality_layer(session)
        logger.debug("Quality layer initialized")
    except ImportError:
        logger.debug("Quality layer not available (Dev C not integrated)")

    return session"""

PATCH_MARKER_1_NEW = """    try:
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

    return session"""


PATCH_MARKER_2_OLD = '''    elif command == "/help":'''

PATCH_MARKER_2_NEW = '''    elif command == "/trace":
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

    elif command == "/help":'''


def apply_patch(cli_py_path: str) -> bool:
    """
    将补丁应用到 forge/cli.py。幂等：已打过补丁则跳过。
    返回 True 表示成功打了补丁，False 表示已经打过或失败。
    """
    from pathlib import Path

    p = Path(cli_py_path)
    if not p.exists():
        print(f"ERROR: {cli_py_path} not found")
        return False

    source = p.read_text(encoding="utf-8")
    if "from .observability import init_observability_layer" in source:
        print("Already patched.")
        return False

    if PATCH_MARKER_1_OLD not in source:
        print("ERROR: Patch marker 1 not found in cli.py — source may have been modified.")
        return False
    if PATCH_MARKER_2_OLD not in source:
        print("ERROR: Patch marker 2 not found in cli.py — source may have been modified.")
        return False

    source = source.replace(PATCH_MARKER_1_OLD, PATCH_MARKER_1_NEW)
    source = source.replace(PATCH_MARKER_2_OLD, PATCH_MARKER_2_NEW, 1)

    p.write_text(source, encoding="utf-8")
    print(f"✓ Patched {cli_py_path}")
    return True


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "forge/cli.py"
    apply_patch(target)
