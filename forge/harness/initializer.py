"""
initializer.py — Initializer Agent

首次运行时（features.json 不存在），分析项目结构，
将高层需求分解为结构化的 feature list（features.json）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Session

logger = logging.getLogger("forge.harness.initializer")

# 常见依赖文件
_DEP_FILES = [
    "package.json", "pyproject.toml", "Cargo.toml",
    "go.mod", "requirements.txt", "pom.xml", "build.gradle",
]

_SYSTEM_PROMPT = (
    "你是一个项目规划专家。根据用户需求和项目现有代码，"
    "将需求分解为有序的 feature 列表。"
)

_USER_TEMPLATE = """## 用户需求
{user_spec}

## 项目现有结构
{directory_tree}

## 现有文件
{extra_context}

## 输出要求
输出一个 JSON 对象，格式如下（不要输出其他任何文字）：
{{
    "version": 1,
    "features": [
        {{
            "id": "feat-001",
            "title": "简短的 feature 标题",
            "status": "pending",
            "acceptance_criteria": ["具体的验收条件1", "条件2"],
            "depends_on": []
        }}
    ]
}}

规则：
- 每个 feature 应该可以在一个上下文窗口内完成（不要太大）
- acceptance_criteria 必须是可验证的具体条件
- 按依赖顺序排列，被依赖的在前
- 通常 5-15 个 feature 为宜"""


async def run_initializer(session: Session, user_spec: str) -> None:
    """
    执行 Initializer Agent 流程。

    1. 收集项目上下文
    2. 调用 LLM 分解需求
    3. 保存 features.json + progress.json
    4. 打印并等待用户确认
    """
    from ..context.progress import save_features, save_progress

    print("\n⚒  Forge Initializer — 分解项目需求为 feature 列表\n")

    # 1. 收集上下文
    directory_tree = await _get_directory_tree(session.project_root)
    extra_context = await _collect_extra_context(session.project_root)

    # 2. 构建 prompt
    user_prompt = _USER_TEMPLATE.format(
        user_spec=user_spec,
        directory_tree=directory_tree,
        extra_context=extra_context,
    )

    # 3. 调用 LLM
    print("正在分析项目并分解需求...")
    try:
        from ..core.query import query_llm

        response = await query_llm(
            model=session.aux_model,
            api_key=session.api_key,
            system_prompt=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            tool_definitions=[],
        )
        raw_text = response.text
    except Exception as e:
        print(f"❌ LLM 调用失败: {e}")
        return

    # 4. 解析 JSON
    features_data = _parse_features_json(raw_text)
    if features_data is None:
        print("❌ 无法解析 LLM 返回的 JSON，请检查模型输出。")
        print(f"原始输出:\n{raw_text[:2000]}")
        return

    # 5. 保存
    save_features(session.forge_dir, features_data)

    initial_progress = {
        "last_session": None,
        "current_feature": None,
        "files_modified": [],
        "tests_status": {},
        "next_steps": "Run forge to start implementing the first feature.",
        "git_branch": "",
        "open_issues": [],
    }
    save_progress(session.forge_dir, initial_progress)

    session.features_data = features_data
    session.progress_data = initial_progress

    # 6. 打印 feature 列表
    features = features_data.get("features", [])
    print(f"\n{'─' * 60}")
    print(f"  分解为 {len(features)} 个 feature:")
    print(f"{'─' * 60}")
    for f in features:
        fid = f.get("id", "?")
        title = f.get("title", "")
        criteria = f.get("acceptance_criteria", [])
        deps = f.get("depends_on", [])
        dep_str = f" (depends: {', '.join(deps)})" if deps else ""
        print(f"  {fid}: {title}{dep_str}")
        for c in criteria:
            print(f"         ✓ {c}")
    print(f"{'─' * 60}")

    # 7. 等待确认
    print(
        "\n以上是分解后的 feature 列表。"
        "输入 'yes' 确认，或手动编辑 .forge/features.json 后重新启动。"
    )
    try:
        answer = input("确认? [yes/no] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "no"

    if answer not in ("yes", "y"):
        print("已取消。请编辑 .forge/features.json 后重新运行 forge。")
        return

    # 8. 可选：创建 git 分支
    if (Path(session.project_root) / ".git").exists():
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "checkout", "-b", "forge/main",
                cwd=session.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                print("✓ 已创建 git 分支: forge/main")
            else:
                # 分支可能已存在
                logger.debug("git checkout -b forge/main failed: %s", stderr.decode())
        except Exception as e:
            logger.debug("Git branch creation failed: %s", e)

    print("\n✓ Initializer 完成。运行 forge 开始实现第一个 feature。\n")


# ── 辅助函数 ─────────────────────────────────────────────────────────────

async def _get_directory_tree(project_root: str, max_depth: int = 2) -> str:
    """获取项目目录树。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "find", project_root, "-maxdepth", str(max_depth),
            "-not", "-path", "*/.git/*",
            "-not", "-path", "*/node_modules/*",
            "-not", "-path", "*/__pycache__/*",
            "-not", "-path", "*/.forge/*",
            cwd=project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        output = stdout.decode("utf-8", errors="replace")
        # 转为相对路径
        root = Path(project_root).resolve()
        lines = []
        for line in output.strip().splitlines():
            try:
                rel = str(Path(line.strip()).resolve().relative_to(root))
                if rel == ".":
                    continue
                lines.append(rel)
            except (ValueError, OSError):
                continue
        return "\n".join(sorted(lines)[:100])
    except Exception as e:
        logger.warning("Failed to get directory tree: %s", e)
        return "(unable to list directory)"


async def _collect_extra_context(project_root: str) -> str:
    """收集 README 和依赖文件内容。"""
    parts: list[str] = []
    root = Path(project_root)

    # README
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = root / name
        if readme.exists():
            try:
                content = readme.read_text(encoding="utf-8")[:3000]
                parts.append(f"### {name}\n{content}")
            except Exception:
                pass
            break

    # 依赖文件
    for dep_file in _DEP_FILES:
        dep_path = root / dep_file
        if dep_path.exists():
            try:
                content = dep_path.read_text(encoding="utf-8")[:2000]
                parts.append(f"### {dep_file}\n{content}")
            except Exception:
                pass

    return "\n\n".join(parts) if parts else "(no additional context files found)"


def _parse_features_json(raw: str) -> dict | None:
    """解析 LLM 返回的 JSON，容错处理 ```json 包裹。"""
    # 尝试直接解析
    text = raw.strip()

    # 去除 markdown 代码块包裹
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None

    # 验证基本结构
    if not isinstance(data, dict) or "features" not in data:
        return None
    if not isinstance(data["features"], list):
        return None

    return data
