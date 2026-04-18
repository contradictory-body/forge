"""
forge.core — 公共数据结构与核心运行时

所有模块通过此处定义的数据结构通信。
"""

from dataclasses import dataclass, field
from typing import Any
from enum import Enum


class SessionMode(Enum):
    PLAN = "plan"
    EXECUTE = "execute"


@dataclass
class ToolResult:
    """所有工具执行的统一返回格式。"""
    content: str
    is_error: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class ToolCall:
    """LLM 返回的工具调用请求。"""
    id: str
    name: str
    arguments: dict


@dataclass
class ToolDefinition:
    """工具注册信息。"""
    name: str
    description: str
    parameters: dict          # JSON Schema
    handler: Any              # async callable(session, arguments) -> ToolResult
    readonly: bool = False
    max_result_chars: int = 30000


@dataclass
class LLMResponse:
    """query.py 返回的 LLM 响应。"""
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class HookResult:
    """钩子执行结果。"""
    inject_content: str | None = None
    abort: bool = False
    abort_reason: str = ""


class PermissionDecision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Session:
    """
    会话状态容器。由 cli.py 创建，传入 loop.run_loop()。
    所有模块通过 Session 共享状态，而非直接相互引用。
    """
    # ── 基础配置 ──
    project_root: str
    forge_dir: str
    mode: SessionMode = SessionMode.EXECUTE
    model: str = "qwen3-coder-plus"       # 代码编写模型（主 agentic loop 使用）
    aux_model: str = "qwen3.5-plus"          # 辅助模型（摘要/评审/编译等非代码任务）
    api_key: str = ""
    max_tokens: int = 200000
    max_iterations: int = 200

    # ── 对话状态 ──
    messages: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    iteration_count: int = 0

    # ── 工具注册表 ──
    tool_registry: dict[str, ToolDefinition] = field(default_factory=dict)

    # ── 钩子注册表 ──
    hooks: dict[str, list] = field(default_factory=lambda: {
        "pre_tool": [],
        "pre_query": [],
        "post_edit": [],
        "post_bash": [],
        "pre_compact": [],
        "on_session_end": [],
        "on_feature_complete": [],
    })

    # ── 权限规则 ──
    permission_rules: dict = field(default_factory=lambda: {
        "deny": [], "ask": [], "allow": []
    })

    # ── Compaction 回调（由 Dev B 注册）──
    compact_callback: Any = None

    # ── 上下文工程状态（由 context/ 模块维护）──
    forge_md_content: str = ""
    auto_memory_content: str = ""
    session_memory_content: str = ""
    progress_data: dict | None = None
    features_data: dict | None = None
    compaction_count: int = 0

    # ── 文件追踪 ──
    recently_read_files: list[str] = field(default_factory=list)

    # ── 杂项元数据 ──
    metadata: dict = field(default_factory=dict)

    @property
    def token_usage_ratio(self) -> float:
        """当前 token 使用率（0.0 ~ 1.0）。"""
        from ..util.token_counter import count_messages_tokens
        estimated = count_messages_tokens(self.messages)
        return min(estimated / self.max_tokens, 1.0) if self.max_tokens > 0 else 0.0
