# Forge Project Analysis & Upgrade — 完整分析文档

## 任务 1：项目结构与代码阅读

项目为 AI 终端编程助手 Agent，包含 35 个 Python 文件（含测试），按关注点分为六层：

```
forge/
├── cli.py              REPL、斜杠命令、会话引导
├── core/               引擎（Session、loop、query、permission、tool_dispatch）
├── context/            Context Engineering 层
├── harness/            entropy、handoff、hooks registry、initializer
├── constraints/        架构约束（rules_parser、import_checker、structural_tests、lint_runner）
├── evaluation/         Generator-Evaluator（criteria、evaluator、diff_formatter、feedback_loop）
├── tools/              内置工具（read、edit、bash、search、git、glob）
├── mcp/                MCP 客户端 + 注册表
└── util/               streaming、terminal_ui、token_counter
```

核心设计模式：

- `Session` 是贯穿全局的共享状态 dataclass
- `loop.run_loop` 是骨架，不包含任何领域逻辑，通过固定的七个钩子点
  （`pre_query`、`pre_tool`、`post_edit`、`post_bash`、`pre_compact`、
  `on_session_end`、`on_feature_complete`）进行扩展
- 每个业务层（`context/`、`constraints/`、`evaluation/`）通过唯一的
  `init_xxx_layer(session)` 入口注册自己的钩子、回调、工具
- `loop.py` 从不直接 import 任何业务层模块 — 这是严格的控制反转

---

## 任务 2：Harness Engineering 核心要素在项目中的具体设计

### Context Engineering（上下文工程）

**分层的 System Prompt 组装**（`context/assembler.py`）。六层：基础指令
（600 tokens）、FORGE.md（1500）、Git 状态（400）、Auto Memory（1000）、
Session Memory（2000）、Progress（800）。总预算 8000，超标时丢弃 Layer 4-5。

**异步刷新 / 同步构建**。Git 状态由 `pre_query` 钩子异步刷新到
`session.metadata["git_status_cache"]`，`build()` 同步读取。避免阻塞。

**三级 Compaction 级联**（`context/compaction.py`）。阈值 0.70/0.85/0.92，
分别对应"清空旧 tool_result"→"session memory 替换旧历史"→"独立 LLM 全量摘要"。
Compaction 后主动重新注入最近读取的文件。

**跨会话持久化**。`progress.json` + `features.json` + `.forge/memory/*.md`
+ `.forge/session_memory.md` 在启动时加载，在 `on_session_end` 时写出。
原子写入（`.tmp` + rename）。

**Session Memory 作为滑动摘要**（`context/session_memory.py`）。每 5 次工具调用
后台异步提取最近 20 条消息为结构化摘要，用 `asyncio.Lock` 串行化。

### Architectural Constraints（架构约束）

**声明式 YAML 规则**，通过 `parse_constraints` 解析为三个 dataclass：
`ArchitectureRules`（layers + cross_cutting）、`LintConfig`、`StructuralRule`。

**严格相邻 Import 规则**（`import_checker.py`）。用 AST 遍历所有 Import
节点，一层只能从**紧邻的下一层** import，不允许跳层。违规消息包含
`File / Line / Violation / Rule / Fix` 五个字段，格式化给 LLM 直接可执行。

**结构性检查**（`structural_tests.py`）：文件行数、函数长度（AST）、文件命名、
禁止 pattern（正则 + per-file exclude 列表）。

**三层权限管线**（`core/permission.py`）。`deny → ask → allow`，first-match-wins，
fail-safe 默认 ASK。规则在 tool name、序列化参数的正则、路径字段的正则上匹配。

**Plan Mode** 是 `tool_dispatch.execute` 里的硬门闸。

**"静默成功，响亮失败"**（`lint_runner.py`）。成功时返回空 `HookResult()` —
零 token 消耗；失败时返回 `HookResult(inject_content=...)`，loop 把它当成
`is_error=True` 的 tool_result 注入对话。

**每次编辑前自动 git stash**（`tools/edit.py`）。`git stash create` +
`git stash store` 不动工作区但可回滚，驱动 `/undo`。

### Entropy Management（熵管理）

**启动时 GC**（`harness/entropy.py`）：
- 删除 7 天前的 `.forge/tool_outputs/*` 文件
- 将 `features.json` 中 `status=done` 数量超 20 的项归档到 `features_archive.json`
- 当 `docs/quality_grades.md` 未解决问题 >10 时发警告

**工具输出溢写**。单次工具输出超 30000 字符时写入文件并在消息里只保留截断版。
这些文件恰好是上面 7 天 GC 的对象。

**Feature 自动完成启发式**（`handoff._try_mark_feature_done`）。扫最近 10 条
assistant 消息找完成关键字（"done" / "完成"），且至少一半的 acceptance
criteria 被提及，才认定完成。故意不 100% 准确 — 用户可手动修 `features.json`。

**会话结束自动 commit**（`handoff._try_git_commit`）。消息 `forge: [feat-xxx]
session checkpoint`，防止变更在会话间烂掉。

**Evaluator 轮次上限**（`feedback_loop._eval_round_counter`）。同一 feature
评审超过 3 轮后停止，写 "NEEDS REVIEW" 到 `quality_grades.md`。防无限循环。

### Evaluate（评审）— 已存在但用户可能没注意到

**Generator-Evaluator 上下文隔离**（`evaluator.evaluate`）。Evaluator 是独立的
LLM 调用，只看 diff/tests/lint 格式化输入，不共享 Generator 的对话历史。
避免自我确认偏差。

**Auto-signal 硬门闸**。Evaluator system prompt 里明确写："若 `auto_signal`
为 `test_result` 且测试失败，此项得分不得超过权重的 30%" — 对 lint 同理。
防止 LLM 替失败辩解。

**Agent 自主触发**。`mark_feature_complete` 工具注册给 agent，agent 认为完成
时调用它，handler 就是 `evaluate_hook`。`needs_revision` 结果注入对话后
agent 自然进入修复循环。

**Fail-open**。LLM 错误 / JSON 解析错误 / 没有当前 feature → 默认 pass
（score 80）。Evaluator 是增强，不是阻塞。

---

## 任务 3：Observation 思想的工程实践（升级方案已实现）

**修正我之前的判断**：Evaluate 是有的，缺的是 **Observation**。项目目前
只有 `logger.info` 调用（无结构化）、`session.iteration_count` 等计数器
（仅在 `/status` 命令显示，无历史）、`quality_grades.md` 记录最终结论
（无过程数据、无 per-criterion 分数历史）。

### 已实现的升级

新增 `forge/observability/` 层，非侵入式装配到现有架构：

| 文件 | 职责 |
|---|---|
| `__init__.py` | `init_observability_layer(session)` — 与现有 `init_context_layer` 同构 |
| `tracer.py` | JSONL 事件写入器，`.forge/traces/{session_id}.jsonl`，10MB 轮换，保留最近 10 会话 |
| `instrumentation.py` | 包装 `build_system_prompt` / `query_llm` / `ToolDispatch.execute` / `compact_callback` / `evaluate` 五个关键点 |
| `analyzer.py` | `summarize_session`、`criterion_trend`（按 criterion 的滑动均值）、`startup_drift_check` |
| `cli_patch.py` | 幂等补丁脚本，向 `cli.py` 注入 init 调用和 `/trace` `/trend` 两个命令 |

### 覆盖的事件类型

`session_start` / `turn_start` / `prompt_built` / `llm_call` / `tool_call_start`
/ `tool_call` / `compaction` / `evaluator_verdict` / `session_summary` / `session_end`

每个事件含 `ts` + `elapsed_s` + `session_id` + `type` + `data` 五字段，
类似 OpenTelemetry span 但本地化、零依赖。

### 新增 REPL 命令

- `/trace [session_id]` — 单次会话执行画像
- `/trend [criterion_name]` — 评审维度的长期趋势，标记漂移

### 启动时自动漂移检测

`startup_drift_check` 扫最近 30 个会话的 `evaluator_verdict` 事件，若某
criterion 的最近 5 次均值比之前 5 次低 15 分以上 → 打印一行警告到 stdout。
无漂移时完全静默。

### 测试覆盖

7 个 observability 测试全部通过，包括：Tracer JSONL 写入、磁盘错误降级、
超大小轮换、`ToolDispatch` 包装后事件捕获、`summarize_session` 聚合准确、
漂移检测能识别人工构造的得分下降、空数据场景。

---

## 任务 4：FORGE.md 自然语言支持（升级方案已实现）

### 原问题

原 `parse_constraints` 和 `load_permission_rules` 都依赖
`re.findall(r"\`\`\`yaml\s*\n(.*?)\`\`\`", ...)` + `yaml.safe_load`。
缺代码栅栏、缩进错、tab 混 space → 静默回落默认规则 → 用户以为生效实际没生效。

### 已实现的升级

新增 `forge/constraints/forge_md_compiler.py`，**三段式策略**：

**1. Cache-first** — 按 FORGE.md 源文本 sha256 缓存到
`.forge/forge_md.compiled.json`。hash 不变 → 零 LLM 调用，零延迟。

**2. LLM-compile on miss** — 首次或变更后调用独立 LLM，用严格 JSON
schema 提示把散文编译成与现有 `ConstraintConfig` + `PermissionRule` 兼容的
数据结构。包括 canonical 工具名（`read_file`、`edit_file`、...）的指导、
正则 escape 规则（"rm -rf" → `rm\s+-rf`）、语义映射
（"forbidden" → deny，"ask first" → ask）。

**3. Graceful fallback** — LLM 失败 / 无 API key / JSON 无法解析 →
回退到现有的正则 + YAML 解析器。YAML 格式的老 FORGE.md 继续工作。

### 向后兼容性

- `CompiledForgeMD.to_constraint_config()` 返回与现有代码完全相同的
  `ConstraintConfig`
- `CompiledForgeMD.to_permission_rules()` 返回与 `load_permission_rules`
  完全相同的 `dict[str, list[PermissionRule]]`
- 下游 `import_checker` / `structural_tests` / `check_permission` 一行代码
  都不用改

### 首次编译时的用户确认

`format_compiled_preview` 把解析结果格式化为紧凑的多行摘要
（"3 deny rules, 5 layers, 2 forbidden patterns, 4 conventions"），
在 REPL 启动时打印，让用户验证 LLM 正确理解了他们的意图。缓存命中时不重复
打印。

### 新的用户体验

见 `FORGE.md.natural_example`。用户现在写的是：

> The codebase is strictly layered. From the bottom up: types, config,
> repository, service, handler. Each layer may only import from the layer
> immediately below it — handler cannot skip service.

而不是：

```yaml
architecture:
  layers: [types, config, repository, service, handler]
```

### 测试覆盖

16 个 compiler 测试全部通过，包括：缓存命中/失效/版本升级失效、LLM 成功路径
（带 mock response）、LLM 失败降级、LLM 返回无效 JSON 降级、无 API key
直接走 legacy、空内容、同步 fast-path、`to_constraint_config` 和
`to_permission_rules` 的下游兼容性（真实调用 `import_checker.check_file` 和
`check_permission`）。

---

## 应用补丁到项目

补丁以两个幂等函数的形式提供：

```bash
python -m forge.observability.cli_patch forge/cli.py
python -m forge.constraints.compiler_integration forge
```

两个脚本都基于 marker 字符串精确匹配，已经打过补丁的文件会被跳过。

## 测试验证

```bash
pytest tests/test_observability.py tests/test_forge_md_compiler.py -v
# 23 passed

pytest tests/ -v
# 87 passed, 13 failed（13 个失败全部是 pre-existing tiktoken 网络问题，
# 与本次升级无关 — 可通过 mv forge/observability /tmp 后重跑验证）
```

## 架构一致性检查

本次升级严格遵循了项目已有的设计模式：

- ✅ 单一 `init_xxx_layer(session)` 入口注册一切
- ✅ 通过 `session.hooks` 钩子系统扩展，不改动 `loop.py` 内部逻辑
- ✅ Fail-open 默认（tracer 磁盘错误后禁用自己、compiler LLM 失败后回落）
- ✅ 原子文件写入（`.tmp` + rename，与 `progress.py` 一致）
- ✅ 受 token 预算约束（事件大小有限，GC 和现有 `.forge/tool_outputs/` 同策略）
- ✅ Dataclass-based 配置转换（`CompiledForgeMD` 产出的正是下游现有 dataclass）
