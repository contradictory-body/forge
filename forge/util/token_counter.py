"""
token_counter.py — Token 计数

优先使用 tiktoken（精确），不可用时退化为字符数 / 4（粗估）。
"""

import logging

logger = logging.getLogger("forge.util.token_counter")

_encoder = None
_tiktoken_available = False

try:
    import tiktoken
    _encoder = tiktoken.get_encoding("cl100k_base")
    _tiktoken_available = True
except ImportError:
    logger.debug("tiktoken not available, falling back to char-based estimation")


def count_tokens(text: str, model: str = "claude-sonnet-4-20250514") -> int:
    """统计文本的 token 数。"""
    if not text:
        return 0
    if _tiktoken_available and _encoder is not None:
        return len(_encoder.encode(text))
    return max(len(text) // 4, 1)


def count_messages_tokens(messages: list[dict]) -> int:
    """统计 messages 列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "") or block.get("content", "")
                    if isinstance(text, str):
                        total += count_tokens(text)
        total += 4  # per-message overhead
    return total
