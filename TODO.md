# Backlog —— ContextForge 待办

> 本文件 = **未做的**。已完成的变更明细见 [PROGRESS.md](./PROGRESS.md)（开发日志），
> 稳定约定见 [CLAUDE.md](./CLAUDE.md)，学习笔记见 [docs/](./docs/)。

## 高优先级 / 下一步

### 多 Agent 协作 Demo：Coordinator → Workers → Reviewer

**定位与目标**

- 这是一个**小而完整、可观察、可真实测试**的学习 Demo，不建设生产级多 Agent 平台。
- 在现有 `Agent` TAOR 内核和 `spawn_subagent` 基础上，真实展示：角色分工、并行 fan-out、上下文隔离、
  结构化交接、Reviewer 独立审查、反馈回流和最终 fan-in。
- 默认演示场景是**只读代码库分析**：Coordinator 把一个分析目标拆给 2～4 个 Worker；Worker 可并行读不同
  文件/维度；Reviewer 回读证据并检查冲突；Coordinator 根据审查结论汇总答案。先避开多 Agent 并发写文件的
  冲突问题。
- **本任务明确不包含长任务编排**；相关想法以后另立任务，不在这里顺手实现。

**一条完整协作链**

1. **Coordinator 规划**：接收用户目标，生成 2～4 个彼此独立、适合并行的 `WorkerTask`；每项写清角色、
   任务边界和预期证据。计划通过专用工具参数提交，由代码校验数量、唯一 ID 和必填字段，而不是解析自由文本。
2. **Workers 并行执行**：代码用有界线程池同时启动独立 `Agent`；每个 Worker 有独立 `messages`、
   `read_files`、LoopDetector 和 trace，只收到自己的任务，不看到兄弟 Worker 的历史。
3. **结构化汇合**：每个 Worker 返回 `WorkerReport`，至少包含 `worker_id / status / summary / evidence /
   trace_ref / usage`；失败、未完成与成功不得再混成普通字符串。
4. **Reviewer 独立审查**：使用新的上下文，只接收原目标和 WorkerReports；允许用只读工具抽查文件证据，输出
   `accept` 或 `revise`，并精确指出需补充的 `worker_id` 和理由。
5. **一次定向补充**：若 Reviewer 要求修订，Coordinator 只重跑被点名的 Worker，并把原任务 + 审查意见交给它；
   最多一轮，防止开放式自我反思无限循环。补充后再审一次。
6. **Coordinator 最终汇总**：只有完成 Worker 汇合和 Reviewer 审查后才生成最终答案；答案标明各结论来自哪个
   Worker/证据，并保留 Reviewer 指出的分歧或不确定项。

**最小角色与协议**

- `Coordinator`：拆分任务、调用协作工具、吸收审查意见、最终汇总；不替 Worker 编造证据。
- `Worker`：完成一个窄任务并提交报告；第一版只暴露只读工具，不允许再次派生 Agent。
- `Reviewer`：独立核验完整性、证据和跨报告冲突；不负责替 Worker 重做整项分析。
- `WorkerTask`：`worker_id`、`role`、`instruction`、`expected_evidence`。
- `WorkerReport`：`worker_id`、`status`、`summary`、`evidence`、`trace_ref`、`usage`、可选 `error`。
- `ReviewReport`：`verdict`（`accept | revise`）、`feedback`、`revise_worker_ids`。
- `TeamResult`：最终答案、最终审查结论、全部 WorkerReports、累计 usage、team trace 路径。

**预计代码边界（实施时以实读代码后的最小改动为准）**

- `agent.py`：补角色 `system_prompt`、结构化运行结果和父子 trace 元数据；保留现有 `run() -> str` 兼容入口。
- 新增 `collaboration.py`：Coordinator/Worker/Reviewer 的专用协作工具、并行执行、一次补充上限和结果聚合。
- `tools.py`：增加按角色选择工具的通用 allowlist；协作工具需要 `tasks[]` 时支持显式嵌套 input schema，
  不把编排逻辑塞进全局工具注册表。
- `cli.py`：增加 `/team <任务>` 演示入口；普通 `contextforge` 单 Agent 流程保持不变。
- `tests/test_collaboration.py`：纯逻辑与并发调度测试；`tests/test_e2e.py`：真实 API 多 Agent 协作测试。

**可观察性**

- 屏幕按事件显示：Coordinator 计划、每个 Worker 启动/完成、并行数、Reviewer verdict、定向补充、最终汇总。
- team trace 记录 `team_id / agent_id / parent_id / role / worker_id / attempt / duration / usage / trace_ref`，
  可从一次 TeamResult 跳到每个 Agent 的既有逐轮 trace。
- 展示团队累计 token，同时保留每个 Agent 的分账；不能只显示 Coordinator 自己的 usage。

**测试与验收（继续遵守「基线 fail → 修复后 pass」）**

- 纯逻辑：任务协议校验、Worker 上下文隔离、至少两个 Worker 真并发、Reviewer 只定向重跑指定 Worker、
  最多一轮补充、失败状态不会伪装成完成、usage/trace 正确聚合。
- 真实 API e2e：让 Coordinator 将本仓库的源码/测试/文档分析拆给多个 Worker；Worker 真读不同文件，Reviewer
  真回读至少一处证据；最终答案必须包含预置的已知事实和对应文件证据。
- e2e 还要断言：至少两个不同 Worker、存在实际并发重叠、各自上下文不泄漏、Reviewer 确实参与、最终汇总使用了
  Worker 结果；不能只断言「返回了非空字符串」。
- 用当前基线运行新增测试并确认失败，再实现至通过；最后跑全量非 e2e 回归和相关真实 API e2e。

**明确不做（防止 Demo 膨胀）**

- 不做任务 DAG、依赖调度、长任务 checkpoint/恢复、分布式队列或跨机器 Worker。
- 不做跨会话长期记忆、向量库/RAG、Agent 自由 peer-to-peer 聊天或多层递归派生。
- 不做多 Agent 并发写代码、自动合并冲突或生产级文件沙箱；第一版协作任务只读。
- 不引入 LangChain/LangGraph，也不用 Anthropic Managed Agents 替代手写协作机制。

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
