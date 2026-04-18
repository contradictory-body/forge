# FORGE.md — Natural Language Test Project

## 项目说明

这是一个用自然语言描述规则的示例项目，用于测试 FORGE.md 自然语言编译器。
项目是一个用 Python 编写的用户管理 API。

## 架构说明

代码库分为五层，从底到顶依次是：types（数据模型）、config（配置）、
repository（数据库访问）、service（业务逻辑）、handler（HTTP 路由）。
util 是通用工具模块，任何层都可以使用它。每一层只能 import
比它低一层的模块，不能跳层。比如 handler 只能调用 service，不能直接调用
repository。

## 操作规则

编辑任何文件之前，必须先用 read_file 读取它。每次修改代码后要运行测试。
一次只做一个功能。完成功能后调用 mark_feature_complete。

## 禁止事项

绝对不允许运行 rm -rf 或 sudo 命令。不能编辑 .env 文件。
代码里不要用 print()，要用 logging 模块代替。

## 需要确认的操作

运行任何 bash 命令之前需要我的确认。git commit 也需要先问我。

## 始终允许的操作

可以随时使用 read_file、search_code、list_directory、git_status、git_diff，
这些只读操作不需要确认。

## 代码风格

每个文件不超过 300 行，每个函数不超过 40 行。
文件名用 snake_case，类名用 PascalCase。
