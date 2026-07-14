# Backlog —— ContextForge 待办

> 本文件 = **未做的**。已完成的变更明细见 [PROGRESS.md](./PROGRESS.md)（开发日志），
> 稳定约定见 [CLAUDE.md](./CLAUDE.md)，学习笔记见 [docs/](./docs/)。

## 低优先级 / 备考

> 均为「可做可不做、暂无硬需求」的项。P1/P2/P3 已完成，明细见 [PROGRESS.md](./PROGRESS.md) 顶部。

### 对照 Claude Code 泄露源码的可借鉴项（2026-07-14 审查）

对照 `claude-code-leak` 的 agent loop / 工具 / 压缩 / 子 agent 机制，逐条比对后列出。
（已确认 myagent 做得对/不吃亏的：验证门做成可插拔 check_command、子 agent 不能再派子 agent、
token 用真实 usage、路径 abspath 归一化——均与 Claude Code 方向一致，无需改。）

- **P4〔存疑，待考证〕micro-compaction（只清旧工具结果，不摘要）**
  - Claude Code 有轻量局部压缩：不调 LLM，只把旧的大工具结果替换成 `[Old tool result content cleared]`、保留最近 N 个。
  - 存疑：对 myagent 是否明确更好尚无硬证据（Claude Code 该逻辑部分在 feature flag 后，外部构建可能不启用）。需更多证据再决定是否做。
- **P5〔可选，优先级低〕大结果「拒绝+提示分页」是否优于「截断头部」**
  - 现状：[agent.py](./src/contextforge/agent.py) 单结果超 5 万字符→砍头保留前 5 万 + 提示分页（阈值与 Claude Code 的 50000 恰好一致）。
  - Claude Code：超限直接报错要求 `offset/limit` 分页（注释「抛错比截断好」，省掉白灌满上下文的浪费）。
  - 判断：现状的截断方案够用，报错的收益对教学项目边际很小。**低优先/可不做**，记录备考。

### 原规划 P6

- **结构化输出 & 跨会话 Memory** —— 原规划的 P6。两块可独立做：
  - 结构化输出：让 agent 能按给定 schema 产出结构化结果（而非只回自然语言）。
  - 跨会话 Memory：把「短期记忆」（当前 `self.messages`，reset 即失忆）升级为可跨会话持久的记忆层。
  - 目前**未开工，优先级低**，视需要再启动。

（新想法随时往这里加。）

## 备注

- **工作流程**：每条 backlog 任务的完整做法（开工前置检查 → 八步闭环 → feature 分支 + PR 收尾）
  统一见 `work-process` skill（[.claude/skills/work-process/SKILL.md](./.claude/skills/work-process/SKILL.md)），
  此处不再重复。稳定约定见 [CLAUDE.md](./CLAUDE.md)「硬约定」。
