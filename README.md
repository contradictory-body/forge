<div align="center">

                                              ```
                                              ███████╗ ██████╗ ██████╗  ██████╗ ███████╗
                                              ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝
                                              █████╗  ██║   ██║██████╔╝██║  ███╗█████╗
                                              ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝
                                              ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗
                                              ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝
                                                ```

**终端原生 AI 编程 Agent，内置 Harness Engineering**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-171_passing-22c55e?style=flat-square)](tests/)
[![Model](https://img.shields.io/badge/Code_Model-qwen3--coder--plus-FF6A00?style=flat-square)](https://dashscope.aliyuncs.com)
[![API](https://img.shields.io/badge/API-DashScope_Compatible-FF6A00?style=flat-square)](https://dashscope.aliyuncs.com)

*规划 → 编码 → 评审 → 学习，闭环自主*

</div>

---

## 什么是 Forge

Forge 是一个运行在终端里的 AI 编程助手，它不只是一个"问答工具"——它能读文件、写代码、运行测试、进行评审，并在多次会话中持续积累对你项目的理解。

Forge 的设计理念来自 **Harness Engineering**：通过精心设计的上下文工程、架构约束、生成-评审循环和可观测性系统，让 Agent 的行为可预测、可审计、可持续改进。

```
你  ──▶  forge  ──▶  规划需求（Plan Mode）
                 ──▶  生成代码（Execute Mode）
                 ──▶  评审质量（Evaluator）
                 ──▶  提炼经验（Experience）
                 ──▶  下次更好（Self-Knowledge）
```

---

## 核心特性

### ⚙️ 双模型路由

Forge 把不同性质的任务分配给最合适的模型：

| 模型 | 用途 |
|------|------|
| `qwen3-coder-plus` | 主 Agentic Loop — 读代码、写代码、运行测试 |
| `qwen3.5-plus` | 所有辅助任务 — 上下文摘要、代码评审、经验提炼、FORGE.md 编译 |

通过 `--model` 和 `--aux-model` 参数可自由切换。

---

### 📋 分层 System Prompt（Context Engineering）

每次 LLM 调用前，Forge 自动组装最多 8000 token 的结构化上下文，共 7 层：

```
Layer 0  基础指令          行为规则、当前模式、项目根目录
Layer 1  FORGE.md          项目级架构规则、权限配置、代码规范
Layer 2  Git 状态           当前分支、最近提交、工作区变更
Layer 3  Auto Memory        跨会话持久学习内容
Layer 4  Session Memory     本次会话压缩摘要
Layer 5  Progress           当前 Feature 进度、待完成任务
Layer 6  Self-Knowledge     从历史评审中提炼的经验教训     ← 独创
```

层级不够用时自动按优先级截断，保证关键上下文始终在场。

---

### 🔄 三级上下文压缩（Compaction）

当对话历史接近 Token 上限时，Forge 自动触发压缩，无需人工干预：

```
ratio > 70%  →  Level 1：清空旧 tool_result 占位，保留结构
ratio > 85%  →  Level 2：用 Session Memory 替换旧历史
ratio > 92%  →  Level 3：LLM 全量摘要，重建精简历史
```

压缩是无损的——关键决策和文件内容会被重新注入。

---

### ✅ Generator-Evaluator 评审循环

每次调用 `mark_feature_complete`，Forge 启动独立的评审上下文，对照 `features.json` 的 `acceptance_criteria` 逐条打分：

```
功能完整性  30 分   所有验收标准是否满足
测试通过    25 分   pytest 退出码、用例数量
架构合规    20 分   层级依赖、命名约定、禁止模式
代码质量    15 分   结构清晰度、错误处理
文档        10 分   docstring 覆盖率
────────────────────
总分         100 分   ≥ 80 分自动通过，< 80 分返回修改指令
```

评审结果写入 `docs/quality_grades.md`，形成可追溯的质量记录。

---

### 🧠 Agent 自我经验系统（Experience）

每次会话结束后，Forge 自动分析 Trace 日志，提炼经验写入 `.forge/experience/`：

```
会话结束
   ↓
分析 evaluator_verdict 事件（失败原因、评分趋势）
   ↓
LLM 提炼 → exp_{timestamp}.md
   ↓
总量超 20000 字符 → 合并为"高可信规律"+"近期观察"
   ↓
下次启动注入 System Prompt Layer 6
```

Agent 会在实际项目经验中学习，避免重复犯同类错误。

---

### 🔍 全链路可观测性（Observability）

每个会话的完整行为链路记录为 JSONL Trace：

```jsonl
{"type": "session_start", "data": {"model": "qwen3-coder-plus", ...}}
{"type": "llm_call",      "data": {"duration_ms": 1823, "input_tokens": 4200, ...}}
{"type": "tool_call",     "data": {"tool": "edit_file", "permission": "allow", ...}}
{"type": "evaluator_verdict", "data": {"verdict": "pass", "score": 92, ...}}
{"type": "session_end",   "data": {"total_elapsed_s": 145.2, ...}}
```

通过 `/trace` 和 `/trend` 命令实时查看，支持漂移检测（连续评分下降自动预警）。

---

### 🏗️ 架构约束引擎（Constraints）

在 `FORGE.md` 里声明架构规则，Forge 在每次文件编辑后自动校验：

```yaml
architecture:
  layers: [types, config, repository, service, handler]
  cross_cutting: [util]

structural:
  max_file_lines: 300
  max_function_lines: 40
  forbidden_patterns:
    - pattern: "print\\("
      message: "Use logging instead of print()"
      exclude: ["cli.py"]
```

违规会被立即注入回 Agent 的上下文，要求修复后才能继续。

---

### 📝 自然语言 FORGE.md 编译

不会写 YAML？用中文描述规则，Forge 自动编译：

```markdown
# FORGE.md（纯自然语言版）

代码分五层，handler 只能调 service，不能跳层调 repository。
不允许用 print()，用 logging 代替，CLI 层除外。
每个文件不超过 300 行，函数不超过 40 行。
bash 命令需要我确认，read_file 和 search_code 随便用。
```

编译结果缓存，内容不变时直接复用。

---

### 📦 features.json 子 Agent

内置专用工具 `write_features_json`，主 Agent 调用后由子 Agent 负责生成规范的项目规划文件。

子 Agent 输出纯文本，**Python 代码负责拼装所有结构字段**——从根本上消除 LLM 格式漂移：

```
LLM 输出（内容）          Python 生成（结构）
──────────────────        ──────────────────────
功能标题                → "id": "feat-001"
验收标准列表            → "status": "in_progress"
依赖序号 1,2            → "depends_on": ["feat-001"]
                        → "version": 1
```

---

## 安装

**系统要求：** Python 3.11+，Git，conda（推荐）

```bash
# 1. 创建 conda 环境
conda create -n forge python=3.11 -y
conda activate forge

# 2. 克隆项目
git clone https://github.com/contradictory-body/forge.git
cd forge

# 3. 安装
pip install -e ".[dev]"
pip install openai  # DashScope 兼容层

# 4. 验证安装
forge --help
```

---

## 配置

### API Key

Forge 通过 DashScope 的 OpenAI 兼容接口调用通义千问系列模型。

**Windows CMD：**
```cmd
set ANTHROPIC_API_KEY=sk-你的DashScope密钥
set OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

**Linux / macOS / Git Bash：**
```bash
export ANTHROPIC_API_KEY=sk-你的DashScope密钥
export OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

永久保存建议写入 `~/.bashrc` 或使用 `setx`（Windows）。

> 在 [阿里云百炼平台](https://bailian.console.aliyun.com/) 申请 DashScope API Key。

---

### FORGE.md

在项目根目录创建 `FORGE.md`，告诉 Forge 你的项目规则。支持 YAML 格式和自然语言两种写法。

**YAML 格式示例：**

```markdown
# FORGE.md

## 架构

​```yaml
architecture:
  layers: [types, config, repository, service, cli]
  cross_cutting: [util]

permissions:
  deny:
    - tool: bash
      pattern: "rm\\s+-rf"
  ask: []
  allow:
    - tool: "*"

structural:
  max_file_lines: 200
  max_function_lines: 30
  forbidden_patterns:
    - pattern: "print\\("
      message: "Use logging"
      exclude: ["cli/"]
​```

## 规范

一次只做一个 feature。完成后调用 mark_feature_complete。
```

---

## 快速开始

### 标准工作流

```bash
cd your-project

# 1. Plan Mode：讨论需求，生成 features.json
forge --plan
You ▸ 我要开发一个任务管理 CLI 工具，纯 Python，五层架构...
You ▸ 帮我生成 features.json
You ▸ /exit

# 2. Execute Mode：逐 Feature 实现
forge
You ▸ 请实现 feat-001
# Forge 自动编码 → 测试 → 调用 mark_feature_complete → Evaluator 评审
# 评审通过后自动推进到 feat-002

You ▸ 请实现 feat-002
# ...

You ▸ /exit  # 自动保存进度，下次从断点继续
```

---

## 命令参考

### 启动参数

```bash
forge                          # Execute Mode（默认）
forge --plan                   # Plan Mode（只读）
forge --model qwen3-coder-plus # 指定代码模型
forge --aux-model qwen3.5-plus # 指定辅助模型
forge --api-key sk-xxx         # 直接传 API Key
forge --max-iterations 300     # 最大迭代次数
forge --verbose                # 开启调试日志
```

### REPL 命令

| 命令 | 说明 |
|------|------|
| `/status` | 查看 Token 用量、模型配置、会话状态 |
| `/trace` | 查看本次会话的执行画像 |
| `/trend` | 查看评审分数历史趋势与漂移预警 |
| `/tools` | 列出所有已注册工具 |
| `/mode plan\|execute` | 切换模式 |
| `/compact` | 手动触发上下文压缩 |
| `/undo` | 撤销最后一次文件编辑（git 快照还原） |
| `/help` | 显示帮助 |
| `/exit` | 退出并保存进度 |

### 中断控制

| 操作 | 效果 |
|------|------|
| `Ctrl+C`（执行中） | 中断当前任务，返回提示符，**不退出** Forge |
| `Ctrl+C`（提示符处） | 退出 Forge |

---

## 内置工具

| 工具 | 权限 | 说明 |
|------|------|------|
| `read_file` | 只读 | 带行号读取文件，支持行范围 |
| `edit_file` | 写 | str_replace 语义精确编辑 |
| `bash` | 写 | 执行 shell 命令，带超时 |
| `search_code` | 只读 | 代码搜索（ripgrep / grep 回退）|
| `list_directory` | 只读 | 目录树展示 |
| `git_status` / `git_diff` | 只读 | Git 状态查看 |
| `git_commit` | 写 | 提交变更 |
| `mark_feature_complete` | 写 | 触发 Evaluator 评审并推进 feature |
| `write_features_json` | 写 | 子 Agent 生成 features.json |

---

## 项目结构

```
forge/
├── forge/
│   ├── cli.py                    # REPL 入口，/命令，Session 构建
│   ├── core/
│   │   ├── __init__.py           # Session dataclass，LLMResponse，ToolCall
│   │   ├── loop.py               # Agentic Loop 主循环
│   │   ├── query.py              # DashScope API 调用（OpenAI 兼容）
│   │   ├── permission.py         # 三级权限管线 deny→ask→allow
│   │   └── tool_dispatch.py      # 工具注册与执行
│   ├── context/
│   │   ├── assembler.py          # 7 层 System Prompt 组装
│   │   ├── compaction.py         # 三级压缩策略
│   │   ├── session_memory.py     # 会话记忆提炼
│   │   └── progress.py           # features.json 读写
│   ├── constraints/
│   │   ├── rules_parser.py       # FORGE.md YAML 解析
│   │   ├── forge_md_compiler.py  # 自然语言 FORGE.md 编译
│   │   ├── import_checker.py     # AST 层级 import 校验
│   │   ├── structural_tests.py   # 文件/函数行数、命名约定
│   │   └── lint_runner.py        # post_edit 钩子触发校验
│   ├── evaluation/
│   │   ├── evaluator.py          # LLM 评审（独立上下文）
│   │   ├── feedback_loop.py      # mark_feature_complete 工具
│   │   ├── criteria.py           # 评分维度定义
│   │   └── diff_formatter.py     # 评审输入格式化
│   ├── observability/
│   │   ├── tracer.py             # JSONL 事件写入器
│   │   ├── instrumentation.py    # 关键路径 monkey-patch
│   │   ├── analyzer.py           # Trace 聚合与漂移检测
│   │   └── experience.py         # 经验提炼、合并、注入
│   ├── harness/
│   │   ├── entropy.py            # 启动时 GC（过期文件清理）
│   │   ├── handoff.py            # 会话结束保存进度
│   │   ├── hooks.py              # 生命周期钩子注册
│   │   └── initializer.py        # 需求分解辅助
│   ├── tools/
│   │   ├── read.py / edit.py / bash.py
│   │   ├── search.py / glob.py / git.py
│   │   └── features_writer.py    # features.json 子 Agent 工具
│   └── mcp/                      # MCP 协议客户端
├── tests/                        # 171 个测试用例
│   └── fixtures/                 # 测试数据集
├── docs/
│   └── quality_grades.md         # 评审质量记录（自动写入）
├── FORGE.md.example              # YAML 格式示例
├── FORGE.md.natural_example      # 自然语言格式示例
└── pyproject.toml
```

---

## 运行测试

```bash
# 全套测试
set PYTHONPATH=. && pytest tests/ -v          # Windows
PYTHONPATH=. pytest tests/ -v                 # Linux/macOS

# 按模块运行
set PYTHONPATH=. && pytest tests/test_permission.py -v
set PYTHONPATH=. && pytest tests/test_evaluator.py -v
set PYTHONPATH=. && pytest tests/test_experience.py -v
set PYTHONPATH=. && pytest tests/test_integration.py -v

# 只跑新增测试（工具层 + 双模型 + 集成）
set PYTHONPATH=. && pytest tests/test_tools.py tests/test_dual_model.py tests/test_integration.py -v
```

**预期结果：171 passed**（网络受限环境下 158 passed，13 个 tiktoken 网络错误属已知问题）

---

## .forge 目录

Forge 在项目根目录下创建 `.forge/` 存储所有运行时数据：

```
.forge/
├── features.json          # Feature 规划与进度
├── progress.json          # 上次会话的进度快照
├── forge_md.compiled.json # FORGE.md 编译缓存
├── traces/                # 会话 JSONL Trace（自动轮换，保留最近 10 个）
│   ├── a1b2c3d4e5f6.jsonl
│   └── a1b2c3d4e5f6.done  # 已提炼标记
├── experience/            # 经验文件（自动合并）
│   └── exp_20260418_142030.md
├── tool_outputs/          # 超长工具输出溢写（7 天自动清理）
└── memory/                # Auto Memory 文件
```

---

## 工作原理简述

```
用户输入
   ↓
cli.py — REPL 读取输入
   ↓
assembler.py — 组装 7 层 System Prompt（含 experience）
   ↓
query.py — 流式调用 DashScope API
   ↓
loop.py — 解析响应（文本 / 工具调用）
   ↓
permission.py — 三级权限检查（deny → ask → allow）
   ↓
tool_dispatch.py — 执行工具
   ↓
lint_runner.py — 编辑后约束校验（post_edit hook）
   ↓
tracer.py — 写入 JSONL Trace（非阻塞）
   ↓
[mark_feature_complete 触发时]
   ↓
evaluator.py — 独立 LLM 上下文评审
   ↓
feedback_loop.py — 更新 features.json，推进或反馈修改指令
   ↓
[会话结束时]
   ↓
experience.py — 提炼 Trace → exp_*.md → 下次注入 Layer 6
```

---

## 常见问题

**Q：启动时卡住不动**

检查 `OPENAI_BASE_URL` 是否设置正确，以及网络是否能访问 DashScope。FORGE.md 编译有 90 秒超时，超时后自动回落到 YAML 解析器。

**Q：`forge: command not found`**

```bash
conda activate forge
pip install -e ".[dev]"
```

**Q：`'export' 不是内部或外部命令`**

你在 Windows CMD 里，用 `set` 代替 `export`：
```cmd
set ANTHROPIC_API_KEY=sk-xxx
```

**Q：features.json 里 feature 状态没有推进**

确认 `forge/evaluation/feedback_loop.py` 是最新版本（包含 `mark_feature_done` + `save_features` 调用）。可查看日志确认有无 `WARNING: Failed to update features.json`。

**Q：Agent 调用了太多次确认**

在 FORGE.md 里把 `ask` 清空，`allow` 改为 `tool: "*"`，删除 `.forge/forge_md.compiled.json` 后重启。

---

## 技术选型说明

| 选择 | 原因 |
|------|------|
| 纯 asyncio，无框架 | 完全掌控事件循环，便于精确插桩 |
| JSONL Trace | 原子追加写入，可增量读取，无需锁 |
| monkey-patch instrumentation | 非侵入式，不改 loop.py 逻辑 |
| 子 Agent 生成内容，Python 生成结构 | 规避 LLM 格式漂移 |
| fail-open Evaluator | 评审失败不阻塞 Agent，降级为 pass |
| 三级压缩 | 轻量优先，只在必要时调用 LLM |

---

## License

MIT © 2026

---

<div align="center">

*"好的工具，用起来像是思维的延伸，而不是障碍。"*

**[⬆ 回到顶部](#)**

</div>
