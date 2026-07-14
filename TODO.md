# Backlog —— ContextForge 待办

> 本文件 = **未做的**。已完成的变更明细见 [PROGRESS.md](./PROGRESS.md)（开发日志），
> 稳定约定见 [CLAUDE.md](./CLAUDE.md)，学习笔记见 [docs/](./docs/)。

## 计划中

### 对照 Claude Code 泄露源码的可借鉴项（2026-07-14 审查）

对照 `claude-code-leak` 的 agent loop / 工具 / 压缩 / 子 agent 机制，逐条比对后列出。
（已确认 myagent 做得对/不吃亏的：验证门做成可插拔 check_command、子 agent 不能再派子 agent、
token 用真实 usage、路径 abspath 归一化——均与 Claude Code 方向一致，无需改。）

- ~~**P1〔真 bug〕并行工具执行不分读写 → 同轮写同一文件是竞态**~~ ✅ **已完成**（part1+part2，见 PROGRESS.md 顶部）
  - part1：`@tool` 加 `concurrency_safe` 标记 + `is_concurrency_safe()`；只读并发、有副作用串行。
  - part2：改为**按原始顺序分组**（相邻只读并发、遇写断开串行），修 part1 的「同轮先写后读读到旧内容」乱序。
- ~~**P2〔明显限制〕`max_tokens=2048` 对编码 agent 过小**~~ ✅ **已完成**（见 PROGRESS.md 顶部）
  - 修法：`MAX_TOKENS_DEFAULT=8192` + `_resolve_max_tokens()`（显式 > `CONTEXTFORGE_MAX_TOKENS` > 默认）；两处调用点改用 `self.max_tokens`。
- **P3〔合理，值得做〕压缩摘要保留「用户原始指令」逐字不改写**
  - 现状：[context.py](./src/contextforge/context.py) 摘要 prompt 保「任务目标/已做/关键结论/下一步」4 维。
  - Claude Code 做法（`services/compact/prompt.ts`）：9 维，特别要求「**所有用户消息**逐字保留」「最近任务逐字引用防漂移」。
  - 借鉴：4 维补一条「保留所有用户原始指令原文」——用户的原话最不该被摘要模型改写。
- **P4〔存疑，待考证〕micro-compaction（只清旧工具结果，不摘要）**
  - Claude Code 有轻量局部压缩：不调 LLM，只把旧的大工具结果替换成 `[Old tool result content cleared]`、保留最近 N 个。
  - 存疑：对 myagent 是否明确更好尚无硬证据（Claude Code 该逻辑部分在 feature flag 后，外部构建可能不启用）。需更多证据再决定是否做。
- **P5〔可选，优先级低〕大结果「拒绝+提示分页」是否优于「截断头部」**
  - 现状：[agent.py](./src/contextforge/agent.py) 单结果超 5 万字符→砍头保留前 5 万 + 提示分页（阈值与 Claude Code 的 50000 恰好一致）。
  - Claude Code：超限直接报错要求 `offset/limit` 分页（注释「抛错比截断好」，省掉白灌满上下文的浪费）。
  - 判断：现状的截断方案够用，报错的收益对教学项目边际很小。**低优先/可不做**，记录备考。

### 原规划 P6（可选）

- **（可选）结构化输出 & 跨会话 Memory** —— 原规划的 P6。两块可独立做：
  - 结构化输出：让 agent 能按给定 schema 产出结构化结果（而非只回自然语言）。
  - 跨会话 Memory：把「短期记忆」（当前 `self.messages`，reset 即失忆）升级为可跨会话持久的记忆层。
  - 目前**未开工，优先级低**，视需要再启动。

（新想法随时往这里加。）

## 备注

- **开发流程（feature 分支 + PR）**：所有改动走 `feat/<任务>` 分支，AI commit + 推 feature 分支 +
  `gh pr create` 开 PR；**main 只能由用户在 GitHub review 后合并 PR**——AI 绝不 push main、不合并 PR
  （用户练 review、把关合并）。见 CLAUDE.md「硬约定」。
- 每个待办收尾 = 实现 + 测试跑绿 + 在 PROGRESS.md 顶部追加一条变更日志 + 开 PR（项目固定规矩）。
