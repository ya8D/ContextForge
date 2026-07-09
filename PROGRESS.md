# 开发日志（CHANGELOG）

> 本文件 = **已完成变更的日志**，时间**倒序**（最新在最上）。
>
> **文档地图**：稳定约定见 [CLAUDE.md](./CLAUDE.md)；未做的待办见 [TODO.md](./TODO.md)；
> 认知性学习笔记见 [docs/](./docs/)；**提交状态以 `git log` 为准**（本文件不记「领先几个提交」）。
>
> 📌 **本项目原名 `myagent`，现更名 `ContextForge`**（包 `contextforge`，命令 `contextforge` / `cf`，
> 环境变量前缀 `CONTEXTFORGE_`）。下方历史条目中出现的 `myagent` / `MYAGENT_` / `P1-P5` / `T1-T6`
> 是**当时的名字/编号**，作为历史记录保留（现已弃用编号，改语义标题；老编号作别名标注帮对应）。

## 演进时间线（从地基到最新，一眼看全貌）

环境准备 → 裸 TAOR 循环 → 多工具/并行/截断/write_file → 自动化测试 → 上下文压缩 →
Harness 三根柱子 → LoopDetector 修正 → Sub-agent → 解开循环 import →
标准包布局 → 快速启动命令 → 日志开关 → 客制化 compact → 简历前缺口补齐 →
**项目更名 ContextForge** → 验证门用户入口 → 指令驱动压缩 → [CF] 前缀+trace 分层 →
验证门 e2e → 学习笔记（docs/ 9 篇）。

---

## 修 5 个真 bug（全局状态泄漏 / 并发 / 健壮性）+ 坦白设计缺口

一份架构级 code review 揪出 12 个问题，逐条核对代码全部属实。修掉 5 个真 bug，各留说明；
#7-#12 是设计权衡，见文末「已知设计权衡 / 缺口」节。

- **#1 READ_FILES 从模块全局收进 Agent 实例**（最严重）：原 `tools.READ_FILES` 是模块级 set、被
  所有 Agent 共享——① reset（新建实例）清不掉它，「先读再改」约束被击穿；② 子 agent 与主 agent
  共享，破坏「两个独立 list 天然隔离」的宣称（messages 隔离了但它没有）；③ 并行执行时并发写竞态。
  **修法（调研了业界做法）**：Claude Agent SDK 用「闭包工厂」（工具是捕获 self 的闭包）、Pydantic AI
  用 `RunContext[Deps]` 依赖注入。闭包工厂要把工具注册表全移进实例、每个 Agent 重注册，**会推翻
  现有模块级自动注册表**（docs/07、08 讲的「放对层」成果），对 4 个工具的学习项目过重。故采用
  **带外参数注入**（思想同 RunContext、更轻）：`@tool` 装饰器跳过下划线前缀参数（不进 schema、
  模型看不到）；`read_file`/`write_file` 加 `_read_files` 参；`execute_tool(name, input, read_files)`
  仅对签名含 `_read_files` 的工具注入；`Agent` 加 `self.read_files=set()`，并行 lambda 传它。
  模块级 `READ_FILES` 降为 `_DEFAULT_READ_FILES` 兜底（脱离 Agent 直接调工具时用）。子 agent/reset/
  并发三个问题一次解决。
- **#2 子 agent trace 目录撞名覆盖**：`run_时分秒` 秒级无唯一标识，一轮里并行派生的多个子 agent
  同一秒创建 → 同一 `run_HHMMSS/` → 各自 task_01 互相覆盖、静默丢 trace。修：加**进程内单调序号**
  后缀 `run_时分秒_序号`。（一开始试过 `id(self)` 低位 hex，但短生命周期实例被回收后 id 会被复用、
  连续创建时仍撞——实测 5 个实例常只剩 1-2 个唯一。改用 `itertools.count()` 单调序号，进程内绝不重复、
  零随机性，符合项目「不用 random/uuid」风格。这是本次一个被测试当场抓出的方案缺陷。）
- **#3 execute_tool 兜坏参数**：`func(**tool_input)` 无 try，模型幻觉出错误参数名 → TypeError 穿透
  `pool.map` → 崩整个 run。修：包 try，参数绑定 TypeError / 其它异常都兜成可读错误串，进模型上下文
  自我纠正而非崩溃。
- **#4 CLI 兜运行异常**：`agent.run(task)` 外层无 try，单次 API 抖动/限流就带 traceback 退出、丢
  全部会话历史。修：包 try/except（不吞 KeyboardInterrupt），报错后回到输入循环、保留 self.messages。
- **#5 loop_detector 每任务清零**：检测器跨 run 复用但 run() 开头不清空，任务2 头几轮指纹和任务1
  结尾混在同一滑动窗口，可能误/漏判。修：run() 开头 `loop_detector.reset()`（死循环是任务内概念，
  区别于有意跨任务保留的 messages/validation_gate 短期记忆）。
- **测试**：`test_tools.py` +4（_read_files 不进 schema、注入集合隔离、execute_tool 注入、坏参数
  返回可读错误不抛）；`test_agent_logic.py` +3（read_files 每实例独立、trace 目录唯一、loop_detector
  每实例独立可清零）。现有 4 处直接调 read_file/write_file 的测试改用 `_DEFAULT_READ_FILES` 兜底。
- **验证**：`not e2e` 99 绿（原 92 + 7）；实证——两 Agent 的 read_files 互不含、schema 只有 `path`；
  连续创建的多个实例 trace 后缀单调序号（0000/0001/…）全不撞（`id(self)` 方案被测试当场证伪后改此）；
  坏参数返回「参数不对」不崩。
- **审查后补修（对这批修复做了 8 角度 code review，揪出两处未到位）**：
  - **#4 脏历史**：原修复只让 CLI「不退出」，但 `run()` 抛异常时 `self.messages` 已 append 了本次
    user 任务、无 assistant 应答——下个任务再 append 就成两条连续 user，污染对话（实测复现）。补修：
    `run()` 拆出 `_run_loop()`，外层 `try/except` 在失败时 `del self.messages[本任务起点:]` 回滚，
    保证「失败=本任务如同没发生」。加测试 `test_run_rolls_back_messages_on_failure`（mock API 抛错，
    断言历史干净、无连续 user）。
  - **#1 只修了一半**：`_DEFAULT_READ_FILES` 兜底集合仍是模块全局——直接调 read_file/write_file
    不传 `_read_files` 时走它，旧的跨调用累积 bug 原样保留，且一个测试仍靠它的副作用串味（注释还
    引用已删的 `READ_FILES`）。收尾：**删掉 `_DEFAULT_READ_FILES`**，`_read_files=None` 时改用
    **一次性空集合**（不跨调用累积）；三个直调测试改为显式传各自的 `set()`。模块全局彻底根除。
  - 已确认无需改的：#2 itertools.count（GIL 下 `next` 原子，实测 8000 并发无撞）、#5 loop reset
    位置正确、子 agent 隔离链路端到端成立。#3（TypeError 捕获偏宽）本轮用户未选，留待日后。

## 学习笔记（docs/ 下 9 篇主题式 + 导览）

- **成果**：把从零手搓这一路的认知性经验沉淀成 9 篇主题笔记，统一结构
  （问题→原来怎么想→真相/原理→学到的道理），标题即结论，篇内互相链接。按主题编排：
  - 基础：01 KV Cache（全量重发为何不贵）
  - 压缩三连：02 本地重写非截断API / 03 该客制化非通用 / 04 指令也过安全判断
  - harness 两讲：05 用代码强制非自律 / 06 死循环 reset 反而放水
  - sub-agent：07 本质是一个工具（隔离是两个 list 免费带来的）
  - 工程反思：08 循环 import 是放错层的信号 / 09 人审与测试互补
- **验证**：`docs/README.md` 导览；所有 README 链接 + 篇内交叉链接核对通过；引用的代码片段
  逐一对照真实源码无虚构。原属 TODO 的 T4 + T5-B，现已完成。

## 验证门真实 e2e（拦住假完成并打回）

- 配一个必失败的检查命令，模型声称完成时验证门反复打回、闭环不放行。断言历史里出现「[验证门]」
  打回 + 循环最终收敛（护栏兜底也算）。
- **意外发现（写进用例注释）**：起初想测「打回→模型建文件修复→通过」，但模型把「凭空建探针文件去
  骗过文件存在性检查」判为**伪造/作弊**而拒绝（受打回消息里「不要蒙混」影响），一路顶到护栏。模型是
  对的——检查的是「文件在不在」而非「真实完成」。故用例只验证门的**拦截职责**、不强求模型作弊——
  这反而印证了「不要蒙混」提示的正效果。
- **验证**：`test_validation_gate_rejects_false_completion` 单独跑绿；`not e2e` 全绿无回归。

## 日志 [CF] 前缀 + trace 按年/月/日分层

- `_log` 所有输出（含 error 级）统一加 `[CF]` 前缀，一眼识别是 ContextForge 打的。
- trace 落盘目录由平铺 `run_<时间戳>` 改为 `traces/<年>/<月>/<日>/run_<时分秒>/`，按日期归档，
  与经典案例目录分层一致。
- **验证**：92 `not e2e` 绿；实跑确认 `[CF] [思考] …` 前缀 + 新 run 落在 `traces/2026/07/09/run_…`。

## 指令驱动压缩（/compact 短会话降级路径 + 禁止补全护栏）

- **起因**：实测会话较短时敲 `/compact 删掉后50名`，结构化压缩切不出中段、返回 None，回「轮数不足以
  压缩」——用户带了明确要求也被拒。用户是有意识地要按自己的方式压，「什么都不做」不合理。
- **最终设计（方案 C）**：新增降级路径，只接主动 `/compact`、且**必须带 directive** 才触发：
  - 保**首条** `messages[0]`（最早的目标锚点），按 directive **压其余** `messages[1:]`，压缩后 =
    `[首条, 摘要]`。prompt 不叠加四维基础要求，完全按 directive（保头已护住锚点）。压缩痕迹由摘要
    marker 承载（含「按要求：<directive>」）。
  - **裸 `/compact`（无 directive）仍不压**；**被动压缩 `_maybe_compact` 不改**。
- **关键教训（几轮迭代才收敛）**：`/compact` 指令**根本不进历史**——cli.py 拦截该命令后直接调
  `compact_now` 再 `continue`，从不 append。故真实短会话（模型直答）历史就是 `[A问题, B回答]` **两条**，
  想压的正是 B。中途试过「保首条 + 保末条 + 压中间」，但「保末条」在两条历史里恰好把要压的 B 保住了、
  压不动（同一条不能既保又压）；且依赖模型是否用了工具（用了才有 ≥3 条），行为不稳定。方案 C
  **保首条 + 压其余**才真正修好：`[A,B]` → `[A, B摘要]`，两条直答型历史也能压。
- **`context.py`**：`compact_by_directive(messages, summarizer, directive)`（保首条 + 压 `messages[1:]`，
  门槛 `len>=2`）+ `_build_directive_only_prompt`（纯指令 prompt）。复用 `_render_middle_for_summary`，
  stats 键与 `compact_messages` 一致、上层打印不分叉。无配对风险。
- **`agent.py`**：`compact_now` 在结构化压缩返回 None **且 directive 非空**时，降级调 `compact_by_directive`。
- **禁止补全护栏（真实 trace 驱动）+ 一条测试教训**：起初担心压缩后模型会补全被删内容——真实 trace
  里发一句 `HI`，模型果然又列出了完整前100名。**但复盘发现根因是测试输入选得不好**：`HI` 太空泛，
  被模型当成「新开场/请继续」，把最初「列前一百」的任务**重答**了一遍，并非「补全被删内容」。换成明确
  只需压缩后数据的问题（「排第一的是谁？只回答这一个」），模型规矩地只答「王」、一个被删名次都没碰
  ——**压缩有效**。教训：验证压缩效果别用空泛输入，那会诱导模型重跑原始任务、造成「没压成」的假象。
  护栏仍保留（摘要消息里加一行「禁止补全、恢复或重新生成被删除的部分」）：对「模型可能好心补全」的
  零成本预防性声明，写在摘要消息本身。不夸大——它减弱该倾向，但模型是否重提旧内容更多取决于后续输入。
- **验证**：`not e2e` 全绿；真实 API 复刻百家姓——`/compact 删掉后五十名`返回「已压缩」，`messages`
  变成 `[A, 摘要]`，保住 A、把排名按要求压掉。归档为经典案例 `classic_cases/2026/07/09/指令驱动压缩-百家姓/`。

## 验证门用户入口（CONTEXTFORGE_CHECK_COMMAND + /check · 承「让机制有变成用户能用」）

- **起因**：更名后重读验证门，发现它和缺口补齐那批是同类漏——`ValidationGate` 逻辑早已接入 TAOR
  循环（声称完成→跑检查→失败打回），但 `check_command` **只有构造参数入口**，CLI 两处都是裸
  `Agent()`。结果：从 `contextforge`/`cf` 跑的用户根本配不了，验证门永远走「未配置→跳过」，形同虚设。
  compact 三兄弟（directive/threshold/executor）都补过环境入口，唯独 `check_command` 漏了。
- **① 环境变量入口** `CONTEXTFORGE_CHECK_COMMAND`：`Agent.__init__` 加兜底「显式 > 环境 > None」，
  与 `compact_directive` 同款。解析后存一份到 `self.check_command`（供 CLI 查看/复用），再建门。
- **② CLI `/check` 命令**：`/check <命令>` 当场设、空 `/check` 查看、`/check off` 清除。设/清都
  **重建 `ValidationGate`**（门的 check_command 是构造时定的只读字段，不给 harness 加 setter）。
  命令存在 Agent 实例上，`reset` 重建实例即清空、回到环境变量默认——语义一致（reset = 彻底重来）。
- **测试**：`test_agent_logic.py` +3（环境兜底且同步进门、显式覆盖环境、默认 None 时门无条件放行）。
- **验证**：`not e2e` 85 绿（原 82 + 3，无回归）；环境变量入口实测 `check_command` 与门内值一致；
  CLI `/check` 设/查/清四步交互全通、`reset` 清掉确认。

## 项目更名（myagent → ContextForge）

**为什么**：`myagent` 只是占位名，已不能反映项目实质——它围绕**超大型代码库（Chromium）**做了实打实的
优化：可客制化的上下文压缩（内容 + 触发阈值双维度可调）、危险 git 命令拦截等。核心特色是「对上下文的
客制化锻造 / 压缩」，故定名 **ContextForge**（`Context` + `Forge`）。

**否掉的备选**：`Architector`——拼写是生造词，且「架构重构 assistant」承诺了项目**并未实现**的能力
（会误导）。`ContextForge` 拼写正确、直指真实核心、不过度承诺。

**改了什么**：
- 包名 `myagent` → `contextforge`（`git mv src/myagent src/contextforge` 保留历史）。
- 命令：注册**两个**入口 `contextforge`（正式）+ `cf`（快捷别名）。
- 环境变量前缀 `MYAGENT_*` → `CONTEXTFORGE_*`（5 个：`LOG` / `TRACE` / `COMPACT_DIRECTIVE` /
  `COMPACT_THRESHOLD` / `COMPACT_EXECUTOR`）。
- `pyproject.toml` 包名 + description + 双 scripts；`.env` 里的实设/注释同步。
- 文档：README / CLAUDE / HIGHLIGHTS 全改为新名；历史明细里当时的 `myagent` / `MYAGENT_` 作为
  「当时的名字」保留不改（改了反而失真）。

**不用改**：`_HERE = .parent.parent.parent`（`src/contextforge/` → `src/` → 根，深度没变）；
物理仓库文件夹仍是 `C:\AI_learning\myagent`（只搬了包目录，没重命名外层文件夹）。

**验证**：`not e2e` 全绿（82）；`cf` / `contextforge` 两命令均可启动；
`grep -rn "myagent\|MYAGENT_" src/ tests/ pyproject.toml` 代码/配置零残留（仅历史文档叙述保留旧名）。

## 简历前缺口补齐（compact 环境入口 + 危险 git 拦截）〔原 T6〕

- **起因**：为写简历逐条核对代码，发现几处「机制实现了、但用户/运行时入口没接全」——
  写进简历要经得起面试追问「怎么操作/怎么触发」，故先补齐。
- **① 压缩触发阈值加环境入口**：`compact_threshold` 之前只是构造参数，从 CLI 起的 agent 调不了。
  新增 `_resolve_compact_threshold`：优先级「显式传参 > `MYAGENT_COMPACT_THRESHOLD` > 默认 500K」，
  非法值（非正整数）兜底回默认不报错。Chromium 这类大项目可调高阈值、用满更多上下文再压。
- **② 压缩执行者加环境入口**：`compact_executor` 同样只能构造传参，CLI 切不到「子 agent 回读核实」。
  加环境兜底 `MYAGENT_COMPACT_EXECUTOR`（self/subagent），优先级「显式 > 环境 > self」——
  现在写 `.env` 一行就能让被动压缩走子 agent 核实，真正成为「用户可选操作」。
- **③ 危险命令拦截补 git 丢工作类**（真实事故驱动：AI 曾把 git 整个 reset、丢了半天工作）：
  `_DANGEROUS_COMMAND_PATTERNS` 新增 `git reset --hard` / `git checkout -- .` / `git checkout .` /
  `git clean -f` / `git push --force` 等模式。刻意只拦「会丢工作」的形态，不误伤安全用法——
  `git checkout <分支名>`（切分支）、`git reset HEAD foo`（unstage）、`git clean -n`（dry-run）均放行。
- **未做的一项（明确决定不做）**：原列的「偷删测试检测接线」——因简历已决定**不写**这条
  （要完全真实），故 `check_test_deletion` 保持现状（有函数有单测、未接进运行时），不强行接线。
- **测试**：`test_harness.py` +2（危险 git 命令全拦、安全 git 用法不误伤）；`test_agent_logic.py` +6
  （阈值默认/环境/显式覆盖/非法值兜底、执行者环境/显式覆盖）。
- **验证**：`not e2e` 81 绿（原 73 + 8，无回归）；代码层实证 harness 硬拦 4 个危险 git 命令、放行 4 个
  安全 git 用法——「用代码强制、不靠模型自觉」（模型碰巧自己拒了也不算数，harness 那道关照挡）。
- **文档**：`.env` 加 `MYAGENT_COMPACT_THRESHOLD` / `MYAGENT_COMPACT_EXECUTOR` 注释示例；
  `CLAUDE.md` 补这两个环境变量说明。

## 客制化 compact（偏好注入 + 执行者切换 + /compact 主动压缩）〔原 T5-A〕

- **动机**：P3 的压缩是"哑"的——`_SUMMARY_PROMPT` 写死四维（目标/已做/发现/下一步），
  `_summarize` 盲总结一次、无工具、无法核实。用户想让压缩带上"当前目的"（保什么、删什么）。
- **设计取舍（与用户确认）**：
  - ① 压缩偏好做成一段自然语言，②裁剪策略（删什么）合进这段话，不单拆成结构化配置。
  - ③ 压缩执行者可切换：默认盲总结，可切成"带工具的子 agent"。
  - **主动 / 被动分流**（用户明确要求）：被动压缩（到阈值自动触发）读会话级预设偏好；
    主动压缩（CLI `/compact <要求>`）用用户当场输入的话，None 时回退到会话级偏好。
- **注入点只有一个**（`context.py`）：`compact_messages` 加 `directive` 参数，经
  `_build_summary_prompt(directive)` 拼 prompt。**directive 为空时逐字等于 P3 的
  `_SUMMARY_PROMPT`（向后兼容钉死）**；有值时把用户要求作为"优先遵守的特别要求"**叠加**在
  四维之上（叠加而非替换——防止用户一句话把"保留任务目标"这种底线也丢了）。摘要消息的标记
  里也附上 directive，回看 trace 时知道这次压缩的目的。
- **`agent.py`**：
  - `Agent.__init__` 加 `compact_directive`（会话级偏好）和 `compact_executor`（"self"/"subagent"）。
  - `_summarize_via_subagent(prompt)`：③ 的执行者——new 一个受限子 agent（复用
    `subagent_tool_schemas()` + max_iterations=15，同 spawn_subagent 构造），把"读懂历史 +
    按要求产出摘要，可用 read_file/run_command 回读核实"作为子任务，返回其最终结论。
  - `_pick_summarizer()`：按 `compact_executor` 选回调；`_maybe_compact`（被动）和
    `compact_now`（主动）都走它。
  - `compact_now(directive=None)`：主动压缩入口，不看 usage 阈值（用户说压就压），
    够轮数就压、返回人类可读结果行；不够则如实说未压。
- **`cli.py`**：输入循环加 `/compact [要求]` 解析（与 reset/exit 同层），banner 补一行帮助。
- **测试**：`test_context.py` +3（directive=None 逐字兼容、directive 注入且四维仍在、
  标记记录 directive）；`test_agent_logic.py` +5（偏好/执行者默认值、_pick_summarizer 切换、
  compact_now 轮数不足/够轮数透传 directive/回退会话级偏好——全用 monkeypatch 假回调不烧钱）。
- **验证**：① `not e2e` 70 绿（62+8，无回归）；② 手动跑：4 步任务攒 10 条历史 →
  `/compact 只保留每个命令输出…` → `已压缩（按要求：…）：消息 10→7 条`；
  ③ **子 agent 执行者手动实证**：构造 `Agent(compact_executor="subagent")`，历史里提到一个探针文件 →
  `compact_now` → 子 agent **真的启动了自己的 TAOR 循环、真的 read_file 回读核实**，摘要里写
  "（已回读 …核实，当前仍成立）"——这正是子 agent 相对盲总结的增量价值（能核实，非凭记忆）。
- **向后兼容**：不传任何该组参数时，`compact_directive=None` + `compact_executor="self"`，
  prompt 逐字等于 P3、执行者就是原 `_summarize`，行为与 P3 完全一致。

## 日志开关（LOG 分级 + TRACE 独立）〔原 T1〕

- **动机**：让用户能开关日志——平时安静，调试时详细；且屏幕输出和 `traces/` 落盘要能分开控制
  （可以「屏幕安静但文件还留着」）。
- **设计取舍**：`MYAGENT_LOG` 用字符串枚举 `off/normal/debug`，不用数字档位
  （如 `MYAGENT_LOG_LEVEL=0/1/2`）——用户提议数字档位，讨论后定字符串：项目风格是
  "宁可啰嗦、追求可读"（CLAUDE.md 硬约定），字符串比数字自解释，也和已有的
  `MYAGENT_TRACE=on/off`（同样字符串）保持统一。
- **改动前先做了一次现状盘点**（Explore agent 核实）：`_log(tag, msg)` 全文件 15 处调用，
  全部只传 2 个位置参数；`tools.py`/`context.py`/`harness.py` 零 print/`_log`；`cli.py` 有
  自己独立的 11 处 `print()`（banner/提示/退出语），和 `_log` 是两套互不相干的输出——
  本次不碰 `cli.py`，只改 `agent.py` 的 TAOR 内部输出。
- **`_log` 加 `level` 参数**（默认 `"normal"`）：内部每次调用都读一次 `MYAGENT_LOG`
  （不缓存，简单正确，换来测试好控制）。`level="error"` 的调用即使 `off` 档也照打；
  `level="debug"` 只在 `MYAGENT_LOG=debug` 才打；非法值兜底当 `normal` 处理，不报错。
- **只标记 4 处为 `error`**（权限拦截 🛡️、死循环 🔁、验证门 🚧、护栏 ⛔），其余 11 处零改动。
  压缩（🗜️ 三处）判为 normal 而非 error：压缩是常规工程行为不是错误，这样 `off` 档"只出答案 +
  错误"的语义才站得住。这就是"业务逻辑一个字不改，只改怎么打日志"。
- **debug 档新增一行**：紧跟 `🧠 [Think]` 之后加 `📊 [debug]` 行，打印
  `current_context_tokens(usage) / self.compact_threshold`，让用户在 debug 档下逐轮看着
  上下文规模逼近压缩阈值。
- **`_trace_enabled()`**：`MYAGENT_TRACE` 默认 `on`，`!= "off"` 即开启。`MYAGENT_TRACE=off` 时
  不仅不写文件，连空目录都不建、也不再打印指向"其实没在写"的路径。
- **测试**：`tests/test_agent_logic.py` 新增 8 个（`_log` 分级 6 个用 `capsys`+`monkeypatch`、
  `_dump_turn`/`_trace_enabled` 2 个构造真实 `Agent()` 指向 `tmp_path`）。
- **验证**：① `not e2e` 62 绿（原 54 + 新增 8）；② 真实三档对比；③ `MYAGENT_TRACE=off` 跑一次，
  `traces/` 目录数量前后不变、`📁 [trace]` announce 行正确消失。
- **文档**：`CLAUDE.md` 新增"日志开关"小节。

## 快速启动（console_scripts 入口 / cf 命令）〔原 T2〕

- **动机**：不想每次用路径找 `main.py`；敲个命令就进交互，对照 `claude` / `pytest` / `black`
  的同款机制。依赖标准包结构已就位。
- **改动**：新增 `pyproject.toml`：
  - `[project.scripts]` 声明入口（console_scripts）。
  - `[tool.setuptools.packages.find] where = ["src"]`——显式告诉 setuptools 包在 `src/` 下，
    否则 src-layout 项目 setuptools 可能找不到包。
  - `dependencies` 照抄 `requirements.txt` 的三个核心依赖；`pytest` 不列入（开发期工具）。
    两份声明目前有点重复，但项目规模小，暂不引入 `[project.optional-dependencies]` 分层
    （不为不存在的问题预先设计）。
- **`py -m pip install -e .`（可编辑安装）**：装进当前环境并注册命令脚本，`-e` 让它指回源码、
  改代码立即生效无需重装。
- **踩到的小陷阱**：`pip install -e .` 会在 `src/` 下生成 `*.egg-info/`（构建元数据），补进 `.gitignore`。
- **验证**：① `py -c "import <包>"` 在任意目录裸执行都成功；② 命令在任意目录直接敲、banner 正常、
  `exit` 正常退出、UTF-8 下中文不乱码；③ `not e2e` 仍 54 绿。
- **文档**：`CLAUDE.md` / `README.md` 运行方式改成"`pip install -e .` 后任意目录敲命令"。

## 标准包 src 布局〔原 T3〕

- **动机**：项目要变成可安装的命令行工具（下一步），需先规范成标准 Python 包布局。
- **改动**：5 个源文件用 `git mv` 收进 `src/<包>/`（保留重命名历史）；`main.py` 改名 `cli.py`。
  新增 `__init__.py`（仅包标记，无 re-export）。import 全部改绝对导入。`tools.py`/`context.py`/
  `harness.py` 不含内部跨模块 import（已确保 tools.py 是纯叶子模块），内容零改动只换位置；
  改动全集中在 `agent.py`（3 处导入 + `_run_check` 内延迟 import）和 `cli.py`（2 处）。
- **踩到的真陷阱**：`agent.py` 里 `_HERE = Path(__file__).parent` 原本定位项目根的 `.env` 和
  `traces/`（`agent.py` 原在项目根，两者恰好重合）。搬进 `src/<包>/` 后 `_HERE` 会变成 `src/<包>/`，
  路径错位两层——这是 pytest **测不出来**的 bug（测试不触发真实 API/落盘），得手工排查移动后的路径
  语义才发现，改成上跳两级修正。提醒：结构性搬迁不能只看 import 报不报错，还要检查每个"相对本文件
  路径"的隐含假设是否还成立。
- **测试文件同步改 import**（7 个）；两个手动演示脚本的 `sys.path.insert` 从指向项目根改成指向 `src/`；
  `00_smoke_test.py` 零改动。**`pytest.ini`**：`pythonpath = .` → `pythonpath = src`。
- **验证分两层**：① `not e2e` 54 绿（无回归）；② `cd src && py -c "import <包>, ..."` 包结构自洽、
  无循环导入。裸 `py -c "import <包>"` 的验收留到装包后一并验证（顺序依赖）。
- **文档同步**：`CLAUDE.md` / `README.md` 目录树与运行方式同步。

## 解开 tools↔agent 循环 import〔原 P5.1〕

- **动机**：用户读 Sub-agent 代码时问「函数内延迟 import Agent 绕开循环，有没有更优雅的解法」。
- **根因**：依赖成环——agent.py 顶部 `from tools import ...`（顺流），而 tools.py 的 spawn_subagent
  需要 Agent（**逆流**，底层反依赖顶层）。延迟 import 只是把逆流边「藏起来晚点走」，没消除它。
- **解法（方案 B，用户已选）**：把 spawn_subagent 从 tools.py **移到 agent.py**（与 Agent 同文件）。
  Agent 就在同文件 → 根本不用 import → 删掉延迟 import 3 行 → **逆流边彻底消除，依赖单向朝下**。
  语义也更对：tools.py = 纯通用工具库；agent.py = Agent + 唯一需要它的工具。
  （没选依赖注入：对单 agent、就一个特殊工具，注入让 execute_tool 调用链变重，不划算。）
- **注册时机变化**：spawn_subagent 移到 agent.py 后，`TOOL_SCHEMAS` 含不含它取决于 **agent 有没有被
  import**——语义上正确（不 import agent 就没有派生能力）。`subagent_tool_schemas` 留在 tools.py
  用「名字过滤」故 agent 导没导都对。
- **测试调整**：`test_tools.py` 的工具集断言改用**子集**（不写死总数，避免依赖全局 import 状态而变脆）；
  `test_subagent.py` 加一条「import agent 后 TOOL_SCHEMAS 含 spawn_subagent」钉死新注册时机。
- **验证**：`py -c "import tools"` 单独导只有 3 工具、不炸；`py -c "import agent"` 无 ImportError；
  test_tools / test_subagent 分别单独跑都绿（证明顺序无关）；54 纯逻辑 + e2e 全绿。
- **收获**：延迟 import 是「能用但治标」，把函数挪到依赖正确的模块才是「根治」——**循环 import 的信号，
  往往是某个东西放错了层**。

## Sub-agent（上下文隔离）〔原 P5〕

- **动机**：到此为止 agent 是**单条上下文**，碰到大任务两个痛点——①上下文污染（读 20 个文件全堆自己
  历史里，又贵又干扰注意力）②噪声回灌（子任务的试错/大段中间输出淹没主线）。压缩治标不治本。
- **解法**：主 agent 把子任务**外包**给一个全新的、上下文隔离的子 agent。子 agent 有自己独立的
  `messages`，自己吭哧跑完（读文件/试错/循环），**只把最终结论**回传。类比：让助理翻 20 份合同、
  只给你一页纸；你的桌面始终干净。
- **关键设计——子 agent 是"一个工具"**：不需要特殊派生机制，把"派生子 agent"做成普通工具
  `spawn_subagent`。主 agent 调它就像调 read_file，工具内部 new 一个 Agent 跑完、返回结论。
- **上下文隔离靠 `new Agent()`**：子 agent = 全新 Agent 实例，`self.messages = []` 是独立空列表，
  和主 agent 两个对象、天然隔离。子 agent 还**自动继承**一切（TAOR/工具/压缩/harness）。
- **两个要防的坑**：① **循环导入**（spawn_subagent 需要 Agent）——见「解开循环 import」；
  ② **无限递归派生**——`subagent_tool_schemas()` 剔除 spawn_subagent，子 agent 只拿基础工具，
  只主 agent 能派生、一层不嵌套，且用更小的 `max_iterations=15`。
- **改动**：`agent.py`（`Agent.__init__` 加 `tools` 参数，默认全集）；`tools.py`（`spawn_subagent`
  工具 + `subagent_tool_schemas()` 受限工具集）。
- **测试**：`tests/test_subagent.py`（5 纯逻辑：两 Agent 的 messages 是独立对象、主全集、子受限、
  默认全集、子更小上限）；`test_tools.py` 更新 + 加受限集测试；`test_e2e.py` 新增 1 个（真派生子 agent）。
- **e2e 实测轨迹（活教材）**：主 agent 调 spawn_subagent → 子 agent 启动**自己独立的 TAOR 循环**
  （有独立 trace 目录）→ 自己跑命令、自己完成 → 只回传一句结论。**主 agent 历史里看不到子 agent 的
  中间步骤**——上下文隔离铁证。这就是 Claude Code 里 Explore/Plan 子 agent 的同款机制。
- **验证**：`not e2e` 53 绿；全套含 e2e 59 绿。

## LoopDetector 修正（人 review 揪出的真 bug）〔原 P4.1〕

- **动机**：用户逐行读 Harness 代码时揪出 `LoopDetector` 两个真实缺陷（都成立，不是过虑）：
  1. **命中后 `reset()` 纵容不听劝的循环**——滑动窗口自己就能"原谅"换动作的模型，reset 反而让
     **不听劝、继续重复**的模型变成"每 3 轮才拦一次、中间 2 轮放水照跑"。用户原话："如果下一轮还是
     一样的，不正是要预防的吗？reset 反而放过了真循环。"
  2. **只取 `tool_use_blocks[0]` 会误判**——方向 A（漏报）：多工具乱序循环 `[读A,读B]`/`[读B,读A]`，
     `[0]` 指纹在 A/B 间交替判不出；方向 B（误报）：第一个工具恰好连续相同但整轮在推进
     （`[跑测试,读日志]`→`[跑测试,写报告]`），`[0]` 全是"跑测试"→ 误打断正干活的模型。
- **修正**：① **删 agent.py 命中后的 `reset()` 调用**（方法保留，是用法错不是方法错）：不听劝→每轮
  都触发（一次不漏）；换动作→滑动窗口自会放行；最坏由 `max_iterations`(100) 兜底。② **指纹改整轮 +
  排序**：新增 `record_round(整轮 blocks)`，内部 `sorted(...)` 压成一个字符串指纹。排序修方向 A、
  整轮参与修方向 B。判据只看请求不含结果。旧 `record`/`is_looping` 签名不动 → 旧测试全不破。
- **测试**：`test_harness.py` 新增 4 个纯逻辑（多工具乱序应判循环、第一个工具同但整轮推进不误判、
  连续重复每轮都触发不自动清零、整轮参数顺序无关）。
- **验证**：`not e2e` 47 绿。**收获**：用户"逐行读代码"揪出了两个自动化测试没覆盖到的设计缺陷——
  **人 review + 测试钉死**是互补的，测试保证"不退化"，人审保证"设计对"。

## Harness 三根柱子（权限拦截 / 死循环检测 / 验证门）〔原 P4〕

- **动机**：前面让 agent「能干活、不撑爆」，但它还太天真——可能 rm -rf 删东西、卡在同一错误反复调、
  嘴上说完成其实测试没跑。补书里六大支柱的**前三根**（书明说这三根解决 80% 可靠性）。
- **本层核心认知**：约束都发生在**我们的代码里**，卡在「模型请求 → 真正执行」之间。模型只「请求」，
  批不批是 harness 说了算。**用代码强制，不靠模型自律**。
- **与书的差异**：书里 ToolRegistry 按「角色」预筛工具集（多 agent 场景）；我们单 agent、工具就 3 个，
  真正风险是「run_command 跑了 rm -rf」而非「该不该给 run_command」，所以做**运行时拦截**。
- **`harness.py`**（三根柱子全是纯逻辑，副作用靠回调注入）：
  - **① 权限分级**：`PermissionLevel` + `TOOL_PERMISSIONS`；`check_command_safety` 正则拦危险命令；
    `check_path_safety` 拦路径遍历和系统目录；`check_tool_call` 统一路由。
  - **② 死循环检测**：`LoopDetector` 滑动窗口记最近 N 次 action 指纹，连续 3 次全相同 → True。
  - **③ 验证门**：`ValidationGate` 声称完成前跑检查命令，输出含 fail/error/错误 → 判未通过打回；
    `check_test_deletion` 防作弊（测试文件删远多于加 → 疑似掏空）。runner 回调注入。
- **接入 `agent.py`**（3 处，都卡在「请求→执行」之间）：执行前每个 tool_use 先过 `check_tool_call`；
  每轮 `record_round` + `is_looping`；完成时先过 `validation_gate.verify` 没过就打回。
- **测试**：`tests/test_harness.py`（17 纯逻辑）+ `test_e2e.py` 新增 2 个（权限拦截、验证门）。
- **测试抓到真 bug**：`chmod -R 777` 没被拦——因 `check_command_safety` 先 `.lower()` 把 `-R` 变
  `-r`，但正则写的大写 `-R`。改成小写 `-r` 修复。这正是写测试的价值（不写就漏了一条危险模式）。
- **e2e 实测轨迹**：①权限——诱导 `rm -rf`，`🛡️ [权限] 拦截`，模型收到拒绝后改口解释为什么不能删；
  ②验证门——配必失败检查命令，模型说「我已完成」，`🚧 [验证门] 未通过，打回` 两次（防完成偏见）。
- **验证**：`not e2e` 43 绿；全套含 e2e 48 绿。

## 上下文压缩（保头 + 压中段 + 保尾）〔原 P3〕

- **要解决的病**：TAOR 每轮都发完整历史（KV Cache 已验证：发出总量 = input + cache_read +
  cache_write），历史越滚越长 → ①质量塌（"lost in the middle"）②钱烧光（全量重发）。
- **压缩 ≠ 截断（两层不同）**：`_truncate_for_feedback` 治「单个」工具结果太大，砍单条；
  `context.py` 治「多轮累积」历史太长，调 LLM 压中段。
- **"压缩 ≠ 截断 API" 的关键澄清**：API 无状态，只认我们每轮发的 messages。所谓压缩，就是我们
  在本地把 `self.messages` **重写**——中段十几轮原文换成一条「前情摘要」消息。控制权全在本地。
- **触发阈值 = 绝对 token 数（非百分比）**：`COMPACT_THRESHOLD_TOKENS = 500_000`。业界依据：Anthropic
  官方 compaction beta 默认 **150K token**（≈1M 的 15%）。本项目自用+学习，主动选激进 500K。
- **token 计数用真实 usage**：`current_context_tokens = input + cache_read + cache_write`。不用
  tiktoken（是 OpenAI 分词器，和 Anthropic 对不上；真实 usage 最准）。
- **压缩策略「保头 + 压中段 + 保尾」**：头（原始任务）必留；尾（最近 `KEEP_RECENT_TURNS=3` 轮）留
  原文；中段调一次 LLM 压成结构化摘要。**按「轮」切分不拆散 tool_use/tool_result**（`_split_into_turns`
  以 assistant 消息为界，拆开 Anthropic API 直接报错）。
- **可测试性设计**：压缩的决策/切分是纯函数；唯一副作用（调 LLM 生成摘要）通过 `summarizer` 回调注入，
  测试传假回调 → 纯逻辑不烧钱。`_summarize` 是真回调（一次无工具无历史的干净调用）。
- **回喂截断上限**：`_MAX_RESULT_CHARS` 8000 → **50000**（1M 大窗口下 8000 太抠）。
- **测试**：`tests/test_context.py`（9 个纯逻辑）+ `test_e2e.py` 新增 1 个压缩触发端到端。
- **e2e 实测（活教材）**：阈值调到 200 逼出压缩。观察到 ①轮数不足时"中段为空，跳过"（护栏生效不假装
  压）；②轮数够了真压「9→8 条」；③**任务不断线**——模型从注入的摘要里读到被压掉的原文信息；④压缩
  改写前缀导致 `cache_read` 回落、`cache_write` 上涨（缓存链断一次的代价）。
- **真实 500K 满配实测**（`tests/02_compaction_demo.py`，造 40 个各 4.5 万字符文件串行读）：上下文从
  0 一轮轮真实涨到 **538968 token**（第 36 轮）→ 真实 500K 阈值触发 → 「30→8 条，压掉 12 轮，摘要
  1052 字符」→ 最终答案「一共读了 40 个文档」**完全正确**（前 32 个原文已压掉，从摘要读回）。
- **验证**：`not e2e` 26 绿；全套含 e2e 29 绿。

## 自动化测试（pytest）〔原 P2.5〕

- **动机**：前面全靠手动跑+肉眼看+删临时文件，无可重复断言测试。趁功能稳定「钉住」。
- **框架**：pytest。配置 `pytest.ini`（`pythonpath` 让 tests 能 import；注册 `e2e` marker；只收集
  `test_*.py`）。
- **测试**：`test_tools.py`（12 个，不烧钱）：装饰器 schema、read_file、write_file 先读再改约束、
  run_command 错误兜底、execute_tool 分发；`test_agent_logic.py`（5 个）：`_truncate_for_feedback`
  截断/边界、`_to_serializable`；`test_e2e.py`（2 个，真调 API）：TAOR 跑通 + 轨迹含 tool_use；
  纯问答一轮结束。
- **跑法**：`py -m pytest -m "not e2e"`（17 绿 / 零烧钱）；`py -m pytest -m e2e`（2 绿 / 真调 API）。
  旧的 `00_smoke_test.py` / `01_run_agent.py` 保留为「手动演示脚本」，文件头注明不是断言测试。

## 多工具 + 并行 + 回喂截断 + write_file〔原 P2〕

- `tools.py` 重构：`@tool` 装饰器从函数签名+docstring **自动生成 input_schema**，告别手写。
- `write_file` 工具 + **「先读再改」硬约束**：没 read_file 读过的已存在文件禁止写（防盲改，对照
  Claude Code FileEditTool）。这是本项目**第一个真正的 harness 约束**——用代码强制，不靠模型自律。
- `agent.py` **并行执行**：一轮多个 tool_use 用 ThreadPoolExecutor 并发（总耗时≈最慢的那个）。
- `agent.py` **回喂截断**（Observe 加厚）：`_truncate_for_feedback` 把回喂的 tool_result 截到 8000
  字符 + 提示分段读取，止住 3 万 token 涌入的爆炸。
- 验证：截断（29万字符→8080）、先读再改（拒绝未读文件/放行新文件）、并行读3文件端到端跑通。

## 裸 TAOR 循环 + KV Cache 调查〔原 P1〕

- `tools.py`：工具层（read_file / run_command + 手写 schema + execute_tool 分发）。
- `agent.py`：核心 TAOR 循环（Think→Act→Observe→Repeat）+ 每轮 trace/log + max_iterations 护栏。
- `tests/01_run_agent.py`：两步任务跑通，完整 TAOR 轨迹可见。`main.py`：正式交互式 CLI 入口。
- **Token 调查 trace**：每轮把「实际发出的 messages + usage 4 字段」落盘到
  `traces/<年>/<月>/<日>/run_<时分秒>/task_NN/turn_NN.json`（按日期分层），循环里同步打印
  `in=.. (cache_read=.., cache_write=..)`。
- **KV Cache 实地验证结论**（用户亲自调查）：`input_tokens` 只是「按全价算的部分」，不等于发出去的
  总量。turn_02 的 `messages_sent` 有 3 条（含 turn_01 全部内容），实际 1958 token 全进了
  `cache_creation_input_tokens`，故 `input_tokens` 只剩 2。**每轮都发完整历史**得证。发出去总量 =
  input + cache_creation + cache_read。
- **观察**：模型第一轮就**并行**请求了 run_command + read_file 两个工具——并行工具调用提前出现，
  下一步把「并行**执行**」正式补上。

## 环境准备 + 项目宪法〔原 P0〕

- 建 `C:\AI_learning\myagent`（与学习仓库平级）；`requirements.txt`（anthropic + tiktoken）；
  `.gitignore`；`CLAUDE.md`（项目宪法）；`PROGRESS.md`（本文件）；
  `tests/00_smoke_test.py` 跑通一次 Anthropic 调用（返回 pong）。

---

## 已知设计权衡 / 缺口

以下是 code review 指出的、**当前有意为之或暂未处理**的设计点。如实记下（不假装不存在），
多数是学习项目的极简取舍，标注「何时该改」以便日后判断。

- **#7 没有 system prompt**：所有指令都塞在 user 任务里，`messages.create` 从不设 `system`。
  意味着没有持久角色约定，「先读再改/不要蒙混」这类护栏只在 harness 硬拦、模型侧完全不知情
  （只能被拒后从 tool_result 反推）。何时该改：想让模型主动遵守约定、减少被拒返工时。
- **#8 `max_tokens=2048` 写死**：Think 调用与压缩摘要都固定 2048。一个能 write_file 的编码 agent，
  单次输出 2048 token 连中等文件都写不全 → 会被截在 `stop_reason=max_tokens`，而循环没对这种截断
  做任何处理（只判 tool_use vs 其它）。何时该改：真要用它写大文件 / 长历史摘要时。
- **#9 验证门对非代码任务也无脑跑检查**：`/check pytest` 后，哪怕问「今天几号」，答完也会强制跑
  pytest；若 pytest 因无关原因失败，纯问答会被反复打回。验证门没有「本任务是否涉及代码」的概念，
  粒度过粗。何时该改：想让验证门只在代码类任务生效时。
- **#10 验证门判定靠子串匹配**：输出含 `fail/error/错误/[未` 即判失败。正常输出里的 `0 errors` /
  `No errors found` 会被误判为失败；`[未` 是为匹配 `[未完成]` 但会误伤任何 `[未…]`。作为「提升成功率
  最大的一环」，判据本身不够可靠。何时该改：想让验证更稳时（如按退出码而非文本）。
- **#11 trace 落盘 O(n²) 磁盘**：每轮都把完整 `messages_sent`（整个历史）写进 `turn_NN.json`，
  长会话第 N 轮的文件含前 N 轮全量 → 总磁盘 ~O(n²)；且 `traces/` 无自动清理。何时该改：长会话/长期
  运行导致磁盘吃紧时（如只存增量、或加保留期清理）。
- **#12 非 `-e` 安装 `.env` 读不到**：`_HERE` 三级上跳假设源码在仓库树里；`pip install .`（非 `-e`）后
  包在 site-packages，`.env` 定位失效。项目约定只用 `-e`（见 CLAUDE.md），可接受但脆。何时该改：
  要正式打包分发时。

---

## 附录：与初版计划的关键决策差异

- **不建虚拟环境**：直接用系统 Python 3.11（`py` 启动器）。
- **凭据改用本地 `.env`**：`ANTHROPIC_AUTH_TOKEN` / `BASE_URL` / `MODEL` 是 VSCode 扩展**只注入给它
  派生的子进程**，普通集成终端拿不到。故落到 `.env`（`.gitignore` 已挡），用 `python-dotenv` 加载。
- **模型 ID 从环境读**，不写死。当前为 `claude-opus-4-8[1m]`。
- **代理端口**：`http://127.0.0.1:23333/api/anthropic`，VSCode 开着时才在（本地服务）。
