"""
terminal_ui.py — 终端 UI

使用 rich 库美化输出。如果 rich 不可用，回退到纯 print。
"""

import json
import sys

try:
    from rich.console import Console
    from rich.theme import Theme
    _console = Console(theme=Theme({
        "assistant": "green",
        "tool": "dim",
        "error": "bold red",
        "warn": "yellow",
        "info": "cyan",
    }))
    _RICH = True
except ImportError:
    _RICH = False

# ── ANSI fallback codes ──
_GREEN = "\033[32m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def print_banner(model: str = "", mode: str = "", tool_count: int = 0) -> None:
    """打印 Forge 启动横幅。"""
    lines = [
        "",
        "  ⚒  Forge — Terminal Coding Agent",
        f"     Model : {model}" if model else "",
        f"     Mode  : {mode}" if mode else "",
        f"     Tools : {tool_count}" if tool_count else "",
        "",
    ]
    text = "\n".join(l for l in lines if l is not None)
    if _RICH:
        _console.print(text, style="info")
    else:
        print(f"{_DIM}{text}{_RESET}")


def print_assistant(text: str) -> None:
    """以绿色前缀 'Forge ▸' 打印助手回复。"""
    if _RICH:
        _console.print(f"[assistant]Forge ▸[/assistant] {text}")
    else:
        print(f"{_GREEN}Forge ▸{_RESET} {text}")


def print_tool_call(name: str, args: dict) -> None:
    """以灰色打印工具调用摘要。"""
    # 简化参数显示：只显示前 80 字符
    args_str = json.dumps(args, ensure_ascii=False)
    if len(args_str) > 80:
        args_str = args_str[:77] + "..."
    if _RICH:
        _console.print(f"  [tool]🔧 {name}({args_str})[/tool]")
    else:
        print(f"  {_DIM}🔧 {name}({args_str}){_RESET}")


def print_tool_result(name: str, result: "ToolResult") -> None:
    """
    成功：灰色 '  ✓ {name} done'
    失败：红色 '  ✗ {name}: {前 200 字符}'
    """
    if result.is_error:
        preview = result.content[:200]
        if _RICH:
            _console.print(f"  [error]✗ {name}: {preview}[/error]")
        else:
            print(f"  {_RED}✗ {name}: {preview}{_RESET}")
    else:
        if _RICH:
            _console.print(f"  [tool]✓ {name} done[/tool]")
        else:
            print(f"  {_DIM}✓ {name} done{_RESET}")


def print_error(msg: str) -> None:
    """红色加粗打印错误。"""
    if _RICH:
        _console.print(f"[error]{msg}[/error]")
    else:
        print(f"{_BOLD}{_RED}{msg}{_RESET}")


def print_info(msg: str) -> None:
    """青色打印信息。"""
    if _RICH:
        _console.print(f"[info]{msg}[/info]")
    else:
        print(f"{_DIM}{msg}{_RESET}")


def ask_permission(tool_call: "ToolCall") -> bool:
    """
    打印权限确认提示，返回用户是否同意。
    """
    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
    if len(args_str) > 120:
        args_str = args_str[:117] + "..."
    prompt = f"  Allow {tool_call.name}({args_str})? [y/N] "
    try:
        if _RICH:
            _console.print(f"[warn]{prompt}[/warn]", end="")
            answer = input().strip().lower()
        else:
            answer = input(f"{prompt}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")
