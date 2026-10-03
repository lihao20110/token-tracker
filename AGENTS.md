# Token Tracker 项目规范

## 定位

本项目统计 Claude Code、Codex、Kimi 等 AI Agent 的 token 用量，并提供 macOS 菜单栏、Sidebar 和 statusline 接入。开始前先读 `ROADMAP.md`；架构、适配器、Sidebar、statusline、定价和主题细则按需读 `docs/agent-handbook.md`。

## 红线与兼容边界

- 不静默替换用户已有 `statusLine`，安装与升级必须保留无关 hooks、settings 和 skills。
- Hook 信任属于用户安全边界，不通过弱化校验、扩大权限或吞掉错误来换取兼容。
- 新增 Agent 接入沿用现有 adapter 边界，不另建平行统计链路；路径常量必须可在测试中隔离。
- 定价解析保持前缀锚定，禁止模糊命中相似模型名。
- 发布、tag、push、改历史和修改密钥必须先确认。

## 按需文档

- 完整工程手册：`docs/agent-handbook.md`
- 项目说明：`README.md`
- 当前进度：`ROADMAP.md`

## 基础验证

```bash
uv run --extra dev pytest
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
LANG=C LC_ALL=C TERM=dumb uv run --extra dev pytest
git diff --check
```
