"""
streaming.py — 流式输出渲染

将 LLM 流式返回的 token 实时打印到终端。
"""

import sys


class StreamRenderer:
    """实时渲染流式 token 到 stdout。"""

    def __init__(self):
        self._buffer = ""
        self._started = False

    def print_token(self, token: str) -> None:
        """实时打印一个 token，不换行。"""
        if not self._started:
            self._started = True
        sys.stdout.write(token)
        sys.stdout.flush()
        self._buffer += token

    def finish(self) -> str:
        """结束流式输出，打印换行，返回累积的完整文本。"""
        if self._started:
            sys.stdout.write("\n")
            sys.stdout.flush()
        result = self._buffer
        self._buffer = ""
        self._started = False
        return result
