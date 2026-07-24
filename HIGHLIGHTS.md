# ContextForge 设计亮点（HIGHLIGHTS）

> 这个项目哪里特别、为什么这么设计——一页速览。开发流水见 [PROGRESS.md](./PROGRESS.md)，
> 稳定约定见 [CLAUDE.md](./CLAUDE.md)。

## harness：用代码强制，不靠模型自律

- **先读再改**：没 `read_file` 读过的已存在文件禁止 `write_file`，防模型基于想象的内容盲改。
  → [tools.py `write_file` / `self.read_files`（Agent 实例状态，带外注入）](./src/contextforge/tools.py)
- **危险命令运行时拦截**：不预筛工具，而是执行前正则拦 `rm -rf` / `format` / `chmod -R 777` 等。
  → [harness.py `check_command_safety`](./src/contextforge/harness.py)
- **验证门防假完成**：模型声称完成时强制跑检查命令，**按退出码判**（0 通过 / 非 0 失败 /
  超时异常视为未完成），没过就打回——对治 LLM 的完成偏见。→ [harness.py `ValidationGate`](./src/contextforge/harness.py)
- **死循环检测按整轮 + 排序指纹**：只看第一个工具会漏报（多工具乱序循环）又误报（首工具相同但
  整轮在推进）；命中后不 reset，避免"每 3 轮才拦一次"放水。→ [harness.py `record_round`](./src/contextforge/harness.py)

## 注入式设计：逻辑与副作用分离

- **副作用靠回调注入，纯逻辑可单测**：`compact_messages` 的 summarizer、`ValidationGate` 的 runner
  都把唯一的副作用（调 LLM / 跑命令）做成可传入回调，测试传假回调即可全覆盖决策逻辑、零 API 调用。
  → [context.py `compact_messages`](./src/contextforge/context.py)
- **sub-agent 就是「一个工具」**：`spawn_subagent` 是普通 `@tool`，内部 `new Agent()` 跑完只回传结论；
  上下文隔离靠两个 Agent 实例的 messages 是独立 list 天然成立。→ [agent.py `spawn_subagent`](./src/contextforge/agent.py)
  （诚实补一句：早期「已读文件」状态曾是**模块级全局**、是这条隔离原则的一个漏网反例——子 agent 会
  和主 agent 共享它。已收进 `Agent.self.read_files`、由 execute_tool 带外注入修正，隔离才真正闭环。）
- **循环 import 是「东西放错层」的信号**：`spawn_subagent` 需要 Agent，故定义在 agent.py 而非 tools.py——
  延迟 import 只治标，挪到依赖正确的层才根治。

## 多 Agent 协作：模型做语义工作，代码控制协作边界

- **不是把 `spawn_subagent` 多调几次**：LLM Coordinator 用实例级 `submit_plan` 交结构化计划；代码校验
  2～4 个任务并有界并行启动独立 Worker；LLM Reviewer 再回读文件抽查证据。→ [collaboration.py](./src/contextforge/collaboration.py)
- **fan-out / fan-in 之间只传结构化报告**：Worker 不看兄弟历史，只交 `WorkerReport`；Reviewer 可精确点名
  返工 Worker，代码强制最多补一次；最终由一个全新的无工具 Aggregator 只根据业务 DTO 汇总，控制流不由模型自由循环。
- **只读不是提示词约定**：每个角色的工具菜单按白名单生成，Agent 执行层还会硬拒绝菜单外工具；
  `submit_plan/report/review` 是实例级 `LocalTool`，并行 Agent 不会在全局注册表里互相覆盖 handler。
- **team trace 保存业务交接 + 运行索引，不复制对话上下文**：`team.json` 记录计划、Worker/Reviewer 结构化报告，
  并为每次角色运行记录 attempt、耗时、usage 和 `trace_ref`；逐轮 messages 仍只存在各 Agent 自己的 trace 中。
  团队累计 token 含主调用、压缩与子 Agent 回传的 usage，各参与者分账同时可见。

## 上下文压缩：本地重写，可客制化

- **压缩 = 在本地重写 messages，不是截断 API**：把中段多轮原文换成一条前情摘要，下轮发出去就短了，
  API 无从知晓。控制权全在本地。→ [context.py `compact_messages`](./src/contextforge/context.py)
- **压缩偏好可注入 + 执行者可切换**：用户能用自然语言指定压缩时保什么删什么；执行者可从「盲总结一次」
  切成「派带工具的子 agent」——后者能 `read_file` 回读核实结论是否还成立，而非凭记忆。
  → [agent.py `_pick_summarizer` / `_summarize_via_subagent`](./src/contextforge/agent.py)
- **压缩指令本身会过模型的安全判断**：措辞越像「系统化删除/隐藏带标识的东西」越可能被拒答；换成
  「降频/去重」的正向表述语义等价却不触发。→ 见 [tests/test_e2e.py](./tests/test_e2e.py) 相关用例

## 可观测性 & 工程细节

- **trace 输入侧 / 输出侧各记各的**：`messages_sent` 是调 LLM 前的输入快照，`response_content` 是本轮
  模型输出，分开存互不污染——单看输入侧曾复盘不了模型回复。→ [agent.py `_dump_turn`](./src/contextforge/agent.py)
- **日志分级用字符串枚举**：`CONTEXTFORGE_LOG=off/normal/debug` 比 `0/1/2` 自解释，且与 `CONTEXTFORGE_TRACE=on/off`
  统一；`error` 级即使 `off` 档也照打。屏幕分级和落盘是两个独立开关。→ [agent.py `_log`](./src/contextforge/agent.py)
- **配置读取一律「显式 > 环境变量 > 默认」**：`model` 读 `ANTHROPIC_MODEL`、`compact_directive` 读
  `CONTEXTFORGE_COMPACT_DIRECTIVE`，写 `.env` 即持久生效，显式传参仍可覆盖。→ [agent.py `Agent.__init__`](./src/contextforge/agent.py)

## 测试哲学

- **测试保证不退化，人审发现设计缺陷**：两处真 bug 都是逐行读代码揪出的，当时自动化测试全绿。
- **测真实 AI 行为用次数对比而非绝对断言**：如验证口癖被压，断言「从 ≥6 次降到 ≤1 次」，既扛得住 LLM
  非确定性，又贴合「降频而非抹净」的真实语义。
