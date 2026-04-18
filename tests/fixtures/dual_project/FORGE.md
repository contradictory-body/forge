# FORGE.md — Dual Project

## 项目概述

一个分层架构的 REST API，用于测试 Forge 的约束引擎。

## 架构

```yaml
architecture:
  layers:
    - types
    - config
    - repository
    - service
    - handler
  cross_cutting:
    - util
```

## 权限

```yaml
permissions:
  deny:
    - tool: bash
      pattern: "rm\\s+-rf"
    - tool: bash
      pattern: "sudo"
    - tool: edit_file
      path: "\\.env$"
  ask:
    - tool: bash
    - tool: git_commit
  allow:
    - tool: read_file
    - tool: search_code
    - tool: list_directory
    - tool: git_status
    - tool: git_diff
```

## Lint

```yaml
lint:
  command: ""

structural:
  max_file_lines: 200
  max_function_lines: 30
  naming:
    files: snake_case
    classes: PascalCase
  forbidden_patterns:
    - pattern: "print\\("
      exclude: []
      message: "Use logging instead of print()"
    - pattern: "TODO(?!\\s*\\[)"
      message: "TODO 必须包含 feature ID，例如 TODO [feat-001]"
```
