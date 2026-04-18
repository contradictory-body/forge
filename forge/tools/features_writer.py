"""
features_writer.py — features.json 生成子 Agent 工具

架构设计（v3）：
  LLM 只负责输出「内容」（feature 标题 + acceptance_criteria 纯文本），
  Python 代码负责拼装「结构」（id 格式、status、version、depends_on）。

  这样做的原因：
    - LLM 擅长理解需求、规划功能、写验收标准
    - LLM 不擅长严格遵守 JSON 字段名/格式规范
    - 把两件事分开，格式问题从根本上消失

流程：
  1. 调用 LLM，得到固定分隔符格式的纯文本
  2. 用正则解析每个 FEATURE 块，提取 title、depends_on、criteria
  3. 由代码生成 id（feat-001..）、status、version，拼装完整 JSON
  4. 验证 criteria 质量，不合格时把具体错误反馈给 LLM 重试
  5. 写入 .forge/features.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..core import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.tools.features_writer")

MAX_RETRIES = 2


# ── 子 Agent System Prompt ────────────────────────────────────────────────
# LLM 只输出纯文本，不输出 JSON，完全规避格式漂移问题

_SYSTEM_PROMPT = """\
你是一个软件项目规划专家。
你的任务是将用户的项目需求拆分成若干个可独立实现的 feature，并为每个 feature 提供具体的验收标准。

输出格式要求：
- 每个 feature 用 "==FEATURE==" 开头
- 第二行写标题（不超过20字）
- 第三行写依赖（格式："DEPENDS: 1,2" 或 "DEPENDS: none"，数字对应 feature 序号）
- 之后每行一条验收标准，以 "- " 开头

示例输出（严格照此格式）：

==FEATURE==
数据模型层
DEPENDS: none
- todo/types.py 存在，包含 Task dataclass、Priority 枚举、Status 枚举
- Task 包含 id(int)、title(str)、priority(Priority)、status(Status)、created_at(str)、due_date(str|None)、notes(str) 字段
- Task.to_dict() 返回可被 json.dumps 序列化的字典
- Task.from_dict(d) 能从 to_dict() 的结果还原出完全相同的 Task 对象
- tests/test_types.py 全部测试通过

==FEATURE==
配置层
DEPENDS: 1
- todo/config.py 存在
- DEFAULT_FILE_PATH 指向用户 home 目录下的 .todo.json
- DEFAULT_PRIORITY 值为 Priority.MEDIUM
- config.py 只 import types 层，不 import 其他项目模块
- tests/test_config.py 全部测试通过

==FEATURE==
数据持久化层
DEPENDS: 2
- todo/repository.py 存在
- load_tasks() 在文件不存在时返回空列表，不抛异常
- load_tasks() 在文件内容为非法 JSON 时返回空列表，不崩溃
- save_tasks() 使用原子写入（先写 .tmp 文件再 rename）
- save_tasks(tasks) 后 load_tasks() 返回内容与原始列表完全一致
- repository.py 只 import config 和 types 层
- tests/test_repository.py 使用 tmp_path fixture，全部测试通过

验收标准规范：
- 每条必须包含以下至少一种具体信息：文件路径、函数名/类名、具体行为、测试文件名、具体异常类型
- 禁止模糊表述：❌"编写相关单元测试" ✅"tests/test_xxx.py 全部测试通过"
- 禁止模糊表述：❌"代码质量良好" ✅"每个函数有 docstring"
- 禁止模糊表述：❌"实现用户友好的输出" ✅"list 命令输出对齐表格，HIGH 优先级标记 [!!]"
- 禁止模糊表述：❌"可导入 types 模块" ✅"service.py 只 import repository 层"
- 每个 feature 至少 3 条验收标准，最多 8 条
- 通常 4-8 个 feature 为宜

只输出 ==FEATURE== 分隔的纯文本，不要输出任何 JSON、代码块或额外说明。\
"""

_USER_TEMPLATE = """\
请根据以下信息规划 feature 列表。

## 项目需求
{user_spec}

## 当前项目结构
{directory_tree}

## 项目规则（来自 FORGE.md）
{forge_md}

## 已有上下文
{extra_context}

请按照系统提示中的格式输出 feature 列表。\
"""


# ── 工具 Handler ─────────────────────────────────────────────────────────

async def _handler(session: Session, arguments: dict) -> ToolResult:
    user_spec: str = arguments.get("spec", "").strip()
    if not user_spec:
        return ToolResult(content="缺少 spec 参数。请提供项目需求描述。", is_error=True)

    # 已存在且有已完成 feature 时拒绝覆盖
    features_path = Path(session.forge_dir) / "features.json"
    if features_path.exists():
        existing = _safe_load_json(features_path)
        done_count = sum(
            1 for f in existing.get("features", [])
            if f.get("status") == "done"
        )
        if done_count > 0:
            return ToolResult(
                content=(
                    f"⚠ .forge/features.json 已存在（{len(existing.get('features',[]))} 个 feature，"
                    f"其中 {done_count} 个已完成）。\n"
                    "如需重新生成，请先备份或删除现有文件后再调用此工具。"
                ),
                is_error=True,
            )

    directory_tree = _get_directory_tree(session.project_root)
    forge_md = session.forge_md_content[:3000] if session.forge_md_content else "（无 FORGE.md）"
    extra_context = _collect_extra_context(session.project_root)

    user_prompt = _USER_TEMPLATE.format(
        user_spec=user_spec,
        directory_tree=directory_tree,
        forge_md=forge_md,
        extra_context=extra_context,
    )

    last_error = ""
    for attempt in range(MAX_RETRIES + 1):
        # 第二次起把上次的错误附加到 prompt
        prompt = user_prompt if attempt == 0 else (
            user_prompt
            + f"\n\n【上次输出有以下问题，请修正后重新输出】\n{last_error}"
        )

        logger.info("Features writer attempt %d/%d", attempt + 1, MAX_RETRIES + 1)

        raw = await _call_llm(session, prompt)
        if raw is None:
            return ToolResult(
                content="子 Agent 调用失败（网络超时或 API 错误），请稍后重试。",
                is_error=True,
            )

        # 解析纯文本 → 结构化列表
        parsed, parse_error = _parse_plain_text(raw)
        if parse_error:
            last_error = parse_error
            logger.warning("Attempt %d parse error: %s", attempt + 1, parse_error)
            continue

        # 验证 criteria 质量
        quality_error = _check_criteria_quality(parsed)
        if quality_error:
            last_error = quality_error
            logger.warning("Attempt %d quality error: %s", attempt + 1, quality_error)
            continue

        # 由代码拼装完整 JSON（格式完全由代码控制）
        features_data = _build_json(parsed)

        # 写入文件
        Path(session.forge_dir).mkdir(parents=True, exist_ok=True)
        tmp_path = features_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(features_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(features_path)
        except Exception as e:
            return ToolResult(content=f"写入 features.json 失败: {e}", is_error=True)

        session.features_data = features_data
        logger.info("features.json written: %d features", len(parsed))

        return ToolResult(
            content=(
                _format_summary(features_data)
                + "\n\n---"
                + "\n⏸ features.json 已生成完毕。请停止，不要自动开始实现任何 feature。"
                + "\n等待用户确认内容后，由用户发出指令再继续。"
            )
        )

    return ToolResult(
        content=(
            f"生成 features.json 失败，已重试 {MAX_RETRIES + 1} 次。\n"
            f"最后一次错误：{last_error}\n\n"
            "【重要】不要使用 edit_file 直接写 features.json 来绕过此工具。"
        ),
        is_error=True,
    )


# ── LLM 调用 ─────────────────────────────────────────────────────────────

async def _call_llm(session: Session, user_prompt: str) -> str | None:
    try:
        from ..core.query import query_llm
        response = await asyncio.wait_for(
            query_llm(
                model=session.aux_model,
                api_key=session.api_key,
                system_prompt=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                tool_definitions=[],
                max_output_tokens=4000,
            ),
            timeout=90.0,
        )
        return response.text
    except asyncio.TimeoutError:
        logger.warning("Sub-agent LLM call timed out (90s)")
        return None
    except Exception as e:
        logger.warning("Sub-agent call failed: %s: %s", type(e).__name__, repr(e))
        return None


# ── 纯文本解析 ───────────────────────────────────────────────────────────

def _parse_plain_text(raw: str) -> tuple[list[dict], str]:
    """
    解析 LLM 输出的 ==FEATURE== 格式纯文本。

    返回 (parsed_list, error_message)。
    parsed_list 中每个元素：
        {"title": str, "dep_indices": list[int], "criteria": list[str]}
    dep_indices 是 1-based feature 序号（依赖的 feature 在列表中的位置）。
    """
    # 按 ==FEATURE== 分块
    blocks = re.split(r"==FEATURE==\s*\n?", raw)
    blocks = [b.strip() for b in blocks if b.strip()]

    if not blocks:
        return [], (
            "输出中没有找到任何 ==FEATURE== 分隔符。"
            "请严格按照示例格式输出，每个 feature 以 ==FEATURE== 开头。"
        )

    parsed: list[dict] = []
    for i, block in enumerate(blocks, start=1):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue

        # 第一行：标题
        title = lines[0].strip()
        if not title:
            return [], f"Feature {i} 的标题为空"

        # 第二行：依赖
        dep_indices: list[int] = []
        criteria_start = 1
        if len(lines) > 1 and lines[1].upper().startswith("DEPENDS:"):
            dep_str = lines[1].split(":", 1)[1].strip()
            if dep_str.lower() not in ("none", "无", ""):
                for part in re.split(r"[,，\s]+", dep_str):
                    part = part.strip()
                    if part.isdigit():
                        dep_num = int(part)
                        if dep_num < 1 or dep_num >= i:
                            return [], (
                                f"Feature {i} 的依赖序号 {dep_num} 非法。"
                                f"依赖只能引用序号更小的 feature（1 到 {i-1}）。"
                            )
                        dep_indices.append(dep_num)
            criteria_start = 2

        # 其余行：验收标准
        criteria: list[str] = []
        for line in lines[criteria_start:]:
            line = line.strip()
            if line.startswith("- "):
                criteria.append(line[2:].strip())
            elif line.startswith("-"):
                criteria.append(line[1:].strip())
            elif line:
                # 没有 "- " 前缀也接受，当作 criteria
                criteria.append(line)

        if not criteria:
            return [], f"Feature {i}「{title}」没有任何验收标准"

        parsed.append({
            "title": title,
            "dep_indices": dep_indices,
            "criteria": criteria,
        })

    if not parsed:
        return [], "解析后没有得到任何 feature，请检查输出格式"

    return parsed, ""


# ── Criteria 质量检查 ─────────────────────────────────────────────────────

SPECIFIC_KEYWORDS = (
    ".py", ".json", ".md", ".txt", ".toml",
    "()", "->", "抛出", "返回", "raises", "returns",
    "tests/", "test_", "pytest",
    "ValueError", "None", "True", "False",
    "import", "只 import", "不 import",
    "[", "]", "标记",
)

VAGUE_STARTERS = (
    "编写相关", "实现用户友好", "添加适当", "确保代码",
    "保证质量", "可导入", "代码质量", "符合规范",
)


def _check_criteria_quality(parsed: list[dict]) -> str:
    """检查所有 feature 的 criteria 质量，返回错误描述或空字符串。"""
    for item in parsed:
        title = item["title"]
        criteria = item["criteria"]

        if len(criteria) < 2:
            return f"「{title}」的验收标准少于 2 条，请补充"

        for c in criteria:
            if len(c) < 8:
                return f"「{title}」有过短的验收标准条目: {c!r}"
            if any(c.startswith(v) for v in VAGUE_STARTERS):
                return (
                    f"「{title}」有模糊的验收标准: {c!r}。"
                    "请改为包含文件名、函数名、具体行为或测试文件名的描述。"
                    "例如：'tests/test_xxx.py 全部测试通过' 而不是 '编写相关单元测试'"
                )

    return ""


# ── JSON 拼装（完全由代码控制格式）────────────────────────────────────────

def _build_json(parsed: list[dict]) -> dict:
    """
    把解析结果拼装成完整的 features.json。
    所有结构字段（id、status、version、depends_on）由代码生成，
    不依赖 LLM 的格式遵从性。
    """
    features = []
    for i, item in enumerate(parsed, start=1):
        fid = f"feat-{i:03d}"
        status = "in_progress" if i == 1 else "pending"
        # dep_indices 是 1-based，转换为 feat-XXX 格式
        depends_on = [f"feat-{d:03d}" for d in item["dep_indices"]]

        features.append({
            "id": fid,
            "title": item["title"],
            "status": status,
            "depends_on": depends_on,
            "acceptance_criteria": item["criteria"],
        })

    return {"version": 1, "features": features}


# ── 摘要 ─────────────────────────────────────────────────────────────────

def _format_summary(data: dict) -> str:
    features = data.get("features", [])
    lines = [f"✅ .forge/features.json 已生成（{len(features)} 个 feature）\n"]
    for f in features:
        icon = "▶" if f.get("status") == "in_progress" else "○"
        deps = f.get("depends_on", [])
        dep_str = f" ← {', '.join(deps)}" if deps else ""
        n = len(f.get("acceptance_criteria", []))
        lines.append(
            f"  {icon} {f['id']}: {f.get('title', '')}{dep_str}  [{n} 条验收标准]"
        )
    current = next(
        (f["id"] for f in features if f.get("status") == "in_progress"),
        features[0]["id"] if features else "?",
    )
    lines.append(f"\n当前应从 {current} 开始实现。")
    return "\n".join(lines)


# ── 辅助函数 ─────────────────────────────────────────────────────────────

def _safe_load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_directory_tree(project_root: str, max_depth: int = 2) -> str:
    EXCLUDE = {".git", "__pycache__", ".forge", "node_modules", ".venv", "venv"}
    try:
        root = Path(project_root)
        lines = [
            str(p.relative_to(root))
            for p in root.rglob("*")
            if not any(x in p.relative_to(root).parts for x in EXCLUDE)
            and len(p.relative_to(root).parts) <= max_depth
        ]
        return "\n".join(sorted(lines)[:80]) or "（空目录）"
    except Exception:
        return "（无法获取目录结构）"


def _collect_extra_context(project_root: str) -> str:
    parts: list[str] = []
    for name in ("README.md", "README.rst", "pyproject.toml", "requirements.txt"):
        f = Path(project_root) / name
        if f.exists():
            try:
                parts.append(f"### {name}\n{f.read_text(encoding='utf-8')[:800]}")
            except Exception:
                pass
    return "\n\n".join(parts) if parts else "（无额外上下文文件）"


# ── 工具定义 ─────────────────────────────────────────────────────────────

TOOL_DEF = ToolDefinition(
    name="write_features_json",
    description=(
        "根据用户的项目需求描述，自动规划并生成 .forge/features.json 文件。"
        "文件包含结构化的 feature 列表，每个 feature 有明确的验收标准和依赖关系。"
        "当用户说'帮我规划 feature'、'生成 features.json'、'制定实现计划'等时调用此工具。"
        "spec 参数应包含用户描述的完整项目需求，越详细越好。"
        "重要：不要用 edit_file 直接写 features.json，必须通过此工具生成。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "spec": {
                "type": "string",
                "description": (
                    "项目需求的完整描述，包括要实现的功能、"
                    "技术栈、架构约束、特殊要求等，越详细越好。"
                ),
            },
        },
        "required": ["spec"],
    },
    handler=_handler,
    readonly=False,
)
