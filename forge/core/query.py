"""
query.py — LLM API 调用封装

使用 OpenAI 兼容接口（支持 DashScope / 任何 OpenAI-compatible 端点）。
通过环境变量 OPENAI_BASE_URL 指定端点。

DashScope 国内站：https://dashscope.aliyuncs.com/compatible-mode/v1
DashScope 国际站：https://dashscope-intl.aliyuncs.com/compatible-mode/v1

错误重试：最多 2 次，指数退避。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING
from uuid import uuid4

from . import LLMResponse, ToolCall
from ..util.streaming import StreamRenderer

if TYPE_CHECKING:
    from . import ToolDefinition

logger = logging.getLogger("forge.core.query")

MAX_RETRIES = 2
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """
    将 Anthropic 格式的消息转换为 OpenAI/DashScope 兼容格式。

    Anthropic 格式的 content 可能是列表，包含 text/tool_use/tool_result 块。
    DashScope 要求：
    - assistant 消息：content 为字符串，tool_calls 为列表
    - tool 结果：role=tool，content 为字符串，tool_call_id 为对应的调用 ID
    - user 消息：content 为字符串
    """
    result: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        # content 是字符串，直接保留
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        # content 是列表（Anthropic 格式），需要转换
        if isinstance(content, list):
            if role == "assistant":
                # 提取文本和工具调用
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        import json as _json
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": _json.dumps(
                                    block.get("input", {}), ensure_ascii=False
                                ),
                            },
                        })
                out: dict = {"role": "assistant", "content": " ".join(text_parts)}
                if tool_calls:
                    out["tool_calls"] = tool_calls
                result.append(out)

            elif role == "user":
                # tool_result 块 → role=tool 消息
                # 普通文本块 → 合并为 user 消息
                text_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "tool_result":
                        tc_content = block.get("content", "")
                        if isinstance(tc_content, list):
                            tc_content = " ".join(
                                b.get("text", "") for b in tc_content
                                if isinstance(b, dict)
                            )
                        result.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": str(tc_content),
                        })
                    elif btype == "text":
                        text_parts.append(block.get("text", ""))
                    else:
                        # 其他类型当文本处理
                        text_parts.append(str(block.get("content", block.get("text", ""))))
                if text_parts:
                    result.append({"role": "user", "content": " ".join(text_parts)})
            else:
                # 其他 role，合并文本
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        texts.append(str(block.get("text", block.get("content", ""))))
                result.append({"role": role, "content": " ".join(texts)})
        else:
            result.append({"role": role, "content": str(content)})

    return result


async def query_llm(
    model: str,
    api_key: str,
    system_prompt: str,
    messages: list[dict],
    tool_definitions: list[ToolDefinition],
    max_output_tokens: int = 16384,
) -> LLMResponse:
    """调用 OpenAI 兼容 API 并返回结构化响应（流式）。"""
    try:
        from openai import AsyncOpenAI, APIStatusError, AuthenticationError, RateLimitError
    except ImportError:
        raise ImportError("openai 包未安装。请运行：pip install openai")

    base_url = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # OpenAI 工具格式
    tools = [
        {
            "type": "function",
            "function": {
                "name": td.name,
                "description": td.description,
                "parameters": td.parameters,
            },
        }
        for td in tool_definitions
    ]

    # system prompt 作为第一条消息
    full_messages = [{"role": "system", "content": system_prompt}] + _normalize_messages(messages)

    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            return await _do_query(client, model, full_messages, tools, max_output_tokens)
        except AuthenticationError:
            raise
        except RateLimitError as e:
            last_error = e
            retry_after = 5
            if hasattr(e, "response") and e.response is not None:
                retry_after = int(e.response.headers.get("retry-after", "5"))
            logger.warning("Rate limited, retrying in %ds (attempt %d)", retry_after, attempt + 1)
            await asyncio.sleep(retry_after)
        except APIStatusError as e:
            last_error = e
            if e.status_code in (500, 502, 503) and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning("API error %d, retrying in %ds", e.status_code, wait)
                await asyncio.sleep(wait)
            else:
                raise
        except Exception:
            raise

    raise last_error  # type: ignore[misc]


async def _do_query(
    client,
    model: str,
    messages: list[dict],
    tools: list[dict],
    max_output_tokens: int,
) -> LLMResponse:
    """执行一次流式 API 调用，使用 create(stream=True)。"""
    renderer = StreamRenderer()
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    pending: dict[int, dict] = {}
    input_tokens = 0
    output_tokens = 0

    kwargs: dict = dict(
        model=model,
        max_tokens=max_output_tokens,
        messages=messages,
        stream=True,
    )
    if tools:
        kwargs["tools"] = tools

    # asyncio.timeout() 覆盖整个流式操作：建立连接 + 全部 chunk 接收
    # Python 3.11+ context-manager 语法，Forge 要求 Python >= 3.11
    async with asyncio.timeout(120.0):
        stream = await client.chat.completions.create(**kwargs)

        async for chunk in stream:
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = chunk.usage.prompt_tokens or input_tokens
                output_tokens = chunk.usage.completion_tokens or output_tokens

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                renderer.print_token(delta.content)
                text_parts.append(delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in pending:
                        pending[idx] = {
                            "id": tc_delta.id or f"call_{uuid4().hex[:8]}",
                            "name": "",
                            "args_json": "",
                        }
                    pc = pending[idx]
                    if tc_delta.id:
                        pc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            pc["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            pc["args_json"] += tc_delta.function.arguments

            if choice.finish_reason in ("tool_calls", "stop") and pending:
                for pc in pending.values():
                    try:
                        args = json.loads(pc["args_json"]) if pc["args_json"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append(ToolCall(
                        id=pc["id"],
                        name=pc["name"],
                        arguments=args,
                    ))
                pending.clear()

    renderer.finish()

    return LLMResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
