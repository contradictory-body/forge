# Forge — Terminal Coding Agent

A lightweight terminal-based coding agent built on Harness Engineering principles.

## Quick Start

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY="your-key"
cd your-project
forge
```

## Architecture

```
forge/
├── cli.py              # CLI entry, REPL, session creation
├── core/               # Engine: agentic loop, LLM query, tool dispatch, permissions
├── tools/              # Built-in tools: read, edit, bash, search, git, glob
├── util/               # Token counting, streaming, terminal UI
├── context/            # (Dev B) Context management, compaction, session handoff
├── constraints/        # (Dev C) Architecture constraints, lint runner
├── evaluation/         # (Dev C) Generator-Evaluator review loop
└── mcp/                # (Dev C) MCP protocol support
```

## Usage

```
forge                   # Start in Execute Mode
forge --plan            # Start in Plan Mode (read-only)
forge --model <name>    # Use a specific model
```

### Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/status` | Show session status |
| `/tools` | List registered tools |
| `/mode plan\|execute` | Switch mode |
| `/compact` | Trigger context compaction |
| `/undo` | Revert last file edit |
| `/exit` | Exit Forge |

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```
