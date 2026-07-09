# 进度（PROGRESS）

> 稳定约定见 [CLAUDE.md](./CLAUDE.md)。本文件只记易变的进度。
>
> 📌 **本项目原名 `myagent`，现更名 `ContextForge`**（包名 `contextforge`，命令 `contextforge` / `cf`，
> 环境变量前缀 `CONTEXTFORGE_`）。下方 P1–P5、T 系列明细中出现的 `myagent` / `MYAGENT_` 是**当时的名字**，
> 作为历史记录保留不改；现在照着跑请用新名。更名详情见文末「项目更名」一节。

## 阶段总览

- [x] **P0 环境准备 + 项目宪法**
- [x] **P1 裸 Agent Loop（TAOR 循环地基）**
- [x] **P2 多工具 + 并行调用 + 回喂截断 + write_file**
- [x] **P2.5 自动化测试（pytest：17 纯逻辑 + 2 端到端）**
- [x] **P3 上下文管理（会话内压缩：保头 + 压中段 + 保尾）**
- [x] **P4 Harness 约束（权限拦截 / 死循环检测 / 验证门）**
- [x] **P5 Sub-agent（上下文隔离：spawn_subagent 工具）**
- [x] **T3 标准包 src 布局**
- [x] **T2 快速启动（console_scripts 入口，`myagent` 命令）**
- [x] **T1 log 开关（MYAGENT_LOG 分级 + MYAGENT_TRACE 独立开关）**
- [x] **T5-A 客制化 compact（压缩偏好可注入 + 压缩执行者可切换 + /compact 主动压缩）**
- [x] **T6 简历前缺口补齐（compact 阈值/执行者环境入口 + 危险 git 命令拦截）**
- [ ] P6（可选）结构化输出 & 跨会话 Memory

## P2 明细

- [x] `tools.py` 重构：`@tool` 装饰器从函数签名+docstring **自动生成 input_schema**，告别手写。
- [x] `write_file` 工具 + **「先读再改」硬约束**：没 read_file 读过的已存在文件禁止写（防盲改，
      对照 Claude Code FileEditTool）。这是本项目**第一个真正的 harness 约束**——用代码强制，不靠模型自律。
- [x] `agent.py` **并行执行**：一轮多个 tool_use 用 ThreadPoolExecutor 并发（总耗时≈最慢的那个）。
- [x] `agent.py` **回喂截断**（Observe 加厚）：`_truncate_for_feedback` 把回喂的 tool_result 截到
      8000 字符 + 提示分段读取，止住 run_20260706_220338 turn_04 那种 3 万 token 涌入的爆炸。
- [x] 验证：截断（29万字符→8080）、先读再改（拒绝未读文件/放行新文件）、并行读3文件端到端跑通。

## P2.5 明细（自动化测试）

- **动机**：P0-P2 全靠手动跑+肉眼看+删临时文件，无可重复断言测试。趁功能稳定「钉住」。
- **框架**：pytest 9.1.1。配置 `pytest.ini`（`pythonpath=.` 让 tests 能 import 上级模块；
  注册 `e2e` marker；只收集 `test_*.py`）。
- **`tests/test_tools.py`**（12 个，不烧钱）：装饰器 schema、read_file、
  write_file 先读再改约束（拒绝/放行/新文件）、run_command 错误兜底、execute_tool 分发。
- **`tests/test_agent_logic.py`**（5 个，不烧钱）：`_truncate_for_feedback` 截断/边界、`_to_serializable`。
- **`tests/test_e2e.py`**（2 个，真调 API，`@pytest.mark.e2e`）：TAOR 跑通 + 轨迹含 tool_use；纯问答一轮结束。
- **跑法**：`py -m pytest -m "not e2e" -v`（17 绿 / 0.78s / 零烧钱）；`py -m pytest -m e2e`（2 绿 / ~14s / 真调 API）。
- 旧的 `00_smoke_test.py` / `01_run_agent.py` 保留为「手动演示脚本」，文件头已注明不是断言测试。

## P3 明细（上下文管理 / 会话内压缩）

- **要解决的病**：TAOR 每轮都发完整历史（KV Cache 已验证：发出总量 = input + cache_read + cache_write），
  历史越滚越长 → ①质量塌（"lost in the middle"）②钱烧光（全量重发）。
- **压缩 ≠ 截断（两层不同）**：`_truncate_for_feedback`（P2）治「单个」工具结果太大，砍单条；
  `context.py`（P3）治「多轮累积」历史太长，调 LLM 压中段。
- **"压缩 ≠ 截断 API" 的关键澄清**：API 无状态，只认我们每轮发的 messages。所谓压缩，就是我们
  在本地把 `self.messages` **重写**——中段十几轮原文换成一条「前情摘要」消息。下轮发过去就短了，
  API 不知道发生过压缩。控制权全在本地 —— 这正是手搓 harness 的意义。
- **触发阈值 = 绝对 token 数（非百分比）**：`COMPACT_THRESHOLD_TOKENS = 500_000`。
  业界依据：Anthropic 官方 compaction beta 默认 **150K token**（≈1M 的 15%），优化质量+成本
  而非"用满窗口"。本项目自用+学习，在知道官方理由后**主动选激进 500K**（用满一半再压）。
- **token 计数用真实 usage**：`current_context_tokens = input + cache_read + cache_write`。
  不用 tiktoken（是 OpenAI 分词器，和 Anthropic 对不上；真实 usage 最准）。
- **压缩策略「保头 + 压中段 + 保尾」**：头（原始任务，第一条）必留；尾（最近 `KEEP_RECENT_TURNS=3` 轮）
  留原文；中段调一次 LLM 压成结构化摘要（目标/已做/发现/下一步）。
- **按「轮」切分不拆散 tool_use/tool_result**：`_split_into_turns` 以 assistant 消息为界切轮，
  保证 tool_use 和配对的 tool_result 不被拆开（拆开 Anthropic API 直接报错）。
- **可测试性设计**：压缩的决策/切分是纯函数；唯一副作用（调 LLM 生成摘要）通过 `summarizer` 回调
  注入，测试传假回调 → 纯逻辑不烧钱。agent 里 `_summarize` 是真回调（一次无工具无历史的干净调用）。
- **回喂截断上限**：`_MAX_RESULT_CHARS` 8000 → **50000**（1M 大窗口下 8000 太抠）。
- **接入点**：TAOR 循环末尾（回喂 tool_result 之后）用本轮 usage 判断 `_maybe_compact`，
  超阈值就压，下一轮 Think 发的就是压缩后历史。
- **测试**：`tests/test_context.py`（9 个纯逻辑）+ `test_e2e.py` 新增 1 个压缩触发端到端。
- **e2e 实测轨迹（活教材）**：阈值调到 200 逼出压缩。观察到：①轮数不足时"中段为空，跳过"
  （护栏生效不假装压）；②轮数够了真压「9→8 条，压掉 1 轮，保留最近 3 轮」；③**任务不断线**——
  模型最后说"加上前情摘要中已完成的第1、2步"，证明它从注入的摘要里读到了被压掉的原文信息；
  ④压缩改写前缀导致 `cache_read` 回落、`cache_write` 上涨（缓存链断一次，符合预期的代价）。
- **跑法**：`py -m pytest -m "not e2e" -v`（26 绿 / ~0.75s）；`py -m pytest -v`（29 绿含 e2e / ~32s）。
- **真实 500K 满配实测**（`tests/02_compaction_demo.py`，造 40 个各 4.5 万字符文件串行读）：
  上下文从 0 一轮轮真实涨到 **538968 token**（第 36 轮）→ 在真实 500K 阈值触发压缩 →
  「消息 30→8 条，压掉 12 轮，保留最近 3 轮，摘要 1052 字符」（50 万 token 历史浓缩成千把字）→
  最终答案「一共读了 40 个文档 doc_00~doc_39」**完全正确**（前 32 个原文已压掉，模型从摘要读回）。
  cache_read 随历史涨（373K→497K），压缩后前缀改写、缓存链断一次重写——KV Cache 联动肉眼可见。
  演示脚本保留可复跑（`--small` 参数走缩小预演），临时大文件跑完已清理。

## P4 明细（Harness 约束 / 三根柱子）

- **动机**：P1-P3 让 agent「能干活、不撑爆」，但它还太天真——可能 rm -rf 删东西、卡在同一错误
  反复调、嘴上说完成其实测试没跑。P4 补书里六大支柱的**前三根**（书明说这三根解决 80% 可靠性）。
- **本层核心认知**：约束都发生在**我们的代码里**，卡在「模型请求 → 真正执行」之间。模型只「请求」，
  批不批是 harness 说了算。**用代码强制，不靠模型自律**——这就是「harness 包着 agent」。
- **与书的差异**：书里 ToolRegistry 按「角色」预筛工具集（多 agent 场景）；我们单 agent、工具就 3 个，
  真正风险是「run_command 跑了 rm -rf」而非「该不该给 run_command」，所以做**运行时拦截**（执行前检查危险动作）。
- **`harness.py`**（新建，三根柱子全是纯逻辑，副作用靠回调注入）：
  - **① 权限分级**：`PermissionLevel`（READ_ONLY/WRITE_SAFE/WRITE_DESTRUCTIVE）+ `TOOL_PERMISSIONS`；
    `check_command_safety` 正则拦危险命令（rm -rf/format/mkfs/shutdown/dd/fork bomb/chmod -R 777…）；
    `check_path_safety` 拦路径遍历（`..`）和系统目录；`check_tool_call` 按工具类型统一路由。
  - **② 死循环检测**：`LoopDetector` 滑动窗口记最近 N 次 action 指纹（参数按 key 排序，顺序无关），
    连续 max_same(=3) 次全相同 → `is_looping()` 返回 True。
  - **③ 验证门**：`ValidationGate` 声称完成前跑检查命令（如 pytest），输出含 fail/error/错误 → 判未通过打回；
    `check_test_deletion` 防作弊（测试文件删远多于加 → 疑似掏空测试）。runner 回调注入（同 summarizer 思路）。
- **接入 `agent.py`**（3 处，都卡在「请求→执行」之间）：
  - 执行前：每个 tool_use 先过 `check_tool_call`，命中危险 → 不执行，拒绝原因当 tool_result 回喂让模型换做法；
  - 每轮：`loop_detector.record_round(整轮工具)` + `is_looping` 检测，鬼打墙就注入换思路提示、`continue`；
  - 完成时：`stop_reason != tool_use` 时先过 `validation_gate.verify`，没过就打回 `continue`，不放行。
  - `Agent.__init__` 加 `check_command` 参数（验证门用）；`_run_check` 回调复用 run_command 跑检查。
- **测试**：`tests/test_harness.py`（17 纯逻辑）+ `test_e2e.py` 新增 2 个（权限拦截、验证门）。
- **测试抓到真 bug**：`chmod -R 777` 没被拦——因 `check_command_safety` 先 `.lower()` 把 `-R` 变 `-r`，
  但正则写的大写 `-R`。改成小写 `-r` 修复。这正是写测试的价值（不写就漏了一条危险模式）。
- **e2e 实测轨迹**：①权限——诱导 `rm -rf`，`🛡️ [权限] 拦截 ... 命中危险命令模式`，模型收到拒绝后
  改口解释为什么不能删（拦截生效+模型收敛，没崩）；②验证门——配必失败检查命令，模型说「我已完成」，
  `🚧 [验证门] 未通过，打回` 两次（防完成偏见，不让嘴上说完成就放行）。
- **跑法**：`py -m pytest -m "not e2e" -v`（43 绿 / ~0.8s）；`py -m pytest -v`（48 绿含 e2e / ~72s）。

## P4.1 明细（LoopDetector 修正 · 用户 review 后提出的真 bug）

- **动机**：用户逐行读 P4 代码时揪出 `LoopDetector` 两个真实缺陷（都成立，不是过虑）：
  1. **命中后 `reset()` 纵容不听劝的循环**——滑动窗口自己就能"原谅"换动作的模型（换动作→窗口混入
     不同指纹→自然 False），reset 没解决真问题，反而让**不听劝、继续重复**的模型变成"每 3 轮才拦一次、
     中间 2 轮放水照跑"。用户原话："如果下一轮还是一样的，不正是要预防的吗？reset 反而放过了真循环。"
  2. **只取 `tool_use_blocks[0]` 会误判**——方向 A（漏报）：多工具乱序循环 `[读A,读B]`/`[读B,读A]`，
     `[0]` 指纹在 A/B 间交替判不出；方向 B（误报）：第一个工具恰好连续相同但整轮在推进
     （`[跑测试,读日志]`→`[跑测试,写报告]`），`[0]` 全是"跑测试"→ 误打断正干活的模型。
- **修正**：
  1. **删 agent.py 命中后的 `reset()` 调用**（方法保留，是用法错不是方法错）。删后：不听劝→每轮都触发
     （一次不漏）；换动作→滑动窗口自会放行；最坏由 `max_iterations`(100) 兜底，不会无限死锁。
  2. **指纹改整轮 + 排序**：`LoopDetector` 新增 `record_round(整轮 blocks)`，内部
     `sorted(_fingerprint(...) for b in blocks)` 压成一个字符串指纹。排序修方向 A（乱序同指纹）、
     整轮参与修方向 B（整轮不同则不同）。`is_looping`（去重看长度）零改动，天然复用。判据只看请求不含结果。
     旧 `record`/`is_looping` 签名不动 → 4 个旧测试全不破。
- **测试**：`test_harness.py` 新增 4 个纯逻辑测试钉死——多工具乱序应判循环（方向A）、
  第一个工具同但整轮推进不误判（方向B）、连续重复每轮都触发不自动清零（reset 洞）、整轮参数顺序无关。
- **跑法**：`py -m pytest -m "not e2e" -v`（47 绿 / ~0.8s）。改动纯逻辑层、单测全覆盖，无需新增 e2e。
- **收获**：用户"逐行读代码"揪出了两个自动化测试没覆盖到的设计缺陷——**人 review + 测试钉死**是互补的，
  测试保证"不退化"，人审保证"设计对"。

## P5 明细（Sub-agent / 上下文隔离）

- **动机**：到 P4 为止 agent 是**单条上下文**，碰到大任务两个痛点——①上下文污染（读 20 个文件全堆自己
  历史里，又贵又干扰注意力）②噪声回灌（子任务的试错/大段中间输出淹没主线）。P3 压缩治标不治本。
- **解法（对照第 9.7 节三层抽象顶层 + 8.2 支柱四）**：主 agent 把子任务**外包**给一个全新的、
  上下文隔离的子 agent。子 agent 有自己独立的 `messages`，自己吭哧跑完（读文件/试错/循环），
  **只把最终结论**回传。类比：让助理翻 20 份合同、只给你一页纸；你的桌面始终干净。
- **关键设计——子 agent 是"一个工具"**：不需要特殊派生机制，把"派生子 agent"做成普通工具
  `spawn_subagent`。主 agent 调它就像调 read_file，工具内部 new 一个 Agent 跑完、返回结论。
  复用了已建好的 Tool 机制（这就是三层抽象里 SubAgent 作为最高层能力的含义）。
- **上下文隔离靠 `new Agent()`**：子 agent = 全新 Agent 实例，`self.messages = []` 是独立空列表，
  和主 agent 两个对象、天然隔离。子 agent 还**自动继承** P1-P4 一切（TAOR/工具/压缩/harness），
  因为它就是个 Agent 实例。
- **两个要防的坑**：
  1. **循环导入**：spawn_subagent 需要 Agent，若放 tools.py 则 `from agent import Agent` 会和
     agent.py 的 `from tools import ...` 成环。**最终解法见 P5.1**（把 spawn_subagent 移到 agent.py，
     与 Agent 同文件、根本不用 import，逆流边彻底消除）。初版曾用「函数内延迟 import」绕过（治标），
     用户 review 后改为 P5.1 的根治方案。
  2. **无限递归派生**：子 agent 若也能 spawn，会层层嵌套失控。解法：`subagent_tool_schemas()` 剔除
     spawn_subagent，子 agent 只拿基础工具（read/run/write）——**只主 agent 能派生、一层不嵌套**。
     子 agent 还用更小的 `max_iterations=15`（子任务不该跑 100 轮）。
- **改动**：`agent.py`（`Agent.__init__` 加 `tools` 参数，默认全集；run() 用 `self.tool_schemas`）；
  `tools.py`（`spawn_subagent` 工具 + `subagent_tool_schemas()` 受限工具集）。
- **测试**：`tests/test_subagent.py`（5 纯逻辑：两 Agent 的 messages 是独立对象、主 agent 全集、
  子 agent 受限、默认全集、子 agent 更小上限）；`test_tools.py` 更新工具数 3→4 + 加受限集测试；
  `test_e2e.py` 新增 1 个（真派生子 agent）。
- **e2e 实测轨迹（活教材，嵌套结构清晰）**：主 agent `🦾 调用 spawn_subagent` → 子 agent 启动**自己独立的
  TAOR 循环**（有独立 trace 目录 run_..._200850，与主 agent 的 ..._200845 是两个会话）→ 子 agent 自己
  `run_command` 跑 echo、自己完成 → 只回传 `[子 agent 完成] SUBAGENT_RESULT_42` 一句给主 agent。
  **主 agent 历史里看不到子 agent 的中间步骤**（那些在子 agent 自己的 messages 里）——上下文隔离铁证。
  这就是 Claude Code 里 Explore/Plan 子 agent 的同款机制。
- **跑法**：`py -m pytest -m "not e2e" -v`（53 绿 / ~2s）；`py -m pytest -v`（59 绿含 e2e / ~100s）。

## P5.1 明细（优雅解开 tools↔agent 循环 import · 用户 review 后提出）

- **动机**：用户读 P5 代码时问「函数内延迟 import Agent 绕开循环，有没有更优雅的解法」。
- **根因**：依赖成环——agent.py 顶部 `from tools import ...`（顺流，agent 用工具），
  而 tools.py 的 spawn_subagent 需要 Agent（**逆流**，底层反依赖顶层）。延迟 import 只是把逆流边
  「藏起来晚点走」，没消除它。依赖本应单向朝下（Agent 是顶层，通用工具是底层）。
- **解法（方案 B，用户已选）**：把 spawn_subagent 从 tools.py **移到 agent.py**（与 Agent 同文件）。
  Agent 就在同文件 → 根本不用 import → 删掉延迟 import 3 行 → **逆流边彻底消除，依赖单向朝下**。
  语义也更对：tools.py = 纯通用工具库（read/run/write，不依赖 Agent）；agent.py = Agent + 唯一需要它的工具。
  （没选依赖注入：对单 agent、就一个特殊工具，注入让 execute_tool 调用链变重，不划算。）
- **注册时机变化**：spawn_subagent 移到 agent.py 后，`TOOL_SCHEMAS` 含不含它取决于 **agent 有没有被 import**
  ——干活工具导 tools 就注册；spawn_subagent 导 agent 才注册。语义上正确（不 import agent 就没有派生能力）。
  `subagent_tool_schemas` 留在 tools.py（过滤逻辑与 TOOL_SCHEMAS 同源），用「名字过滤」故 agent 导没导都对。
- **测试调整**：`test_tools.py` 的工具集断言改用**子集**（`{read,run,write} <= names`），不写死总数
  ——否则依赖「agent 有没有被 import」这个全局状态，pytest 跑全套时 test_subagent 先导 agent 会让断言变脆；
  `test_subagent.py` 加一条「import agent 后 TOOL_SCHEMAS 含 spawn_subagent」钉死新注册时机。
- **验证**：`py -c "import tools"` 单独导只有 3 工具、不炸；`py -c "import agent"` 无 ImportError、spawn 注册；
  test_tools / test_subagent **分别单独跑都绿**（证明顺序无关，避开了测试间全局状态耦合）；
  e2e 派生子 agent 仍绿（重构不改行为）。54 纯逻辑 + e2e 全绿。
- **收获**：延迟 import 是「能用但治标」，把函数挪到依赖正确的模块才是「根治」——**循环 import 的信号，
  往往是某个东西放错了层**（spawn_subagent 属于 Agent 层，却被放进了工具层）。

## T3 明细（标准包 src 布局）

- **动机**：项目要变成可安装的命令行工具（下一步 T2），需先规范成标准 Python 包布局。
- **改动**：5 个源文件用 `git mv` 收进 `src/myagent/`（保留重命名历史）：`agent.py` /
  `tools.py` / `context.py` / `harness.py` 原样搬入；`main.py` 改名为 `cli.py`（T3 要求）。
  新增 `src/myagent/__init__.py`（仅包标记，无 re-export）。
- **import 全部改绝对导入**：`from tools import ...` → `from myagent.tools import ...`
  （不用 `from .tools import ...` 相对导入）。`tools.py`/`context.py`/`harness.py` 本身
  不含任何内部跨模块 import（P5.1 已确保 tools.py 是纯叶子模块），内容零改动，只是物理换位置；
  改动全集中在 `agent.py`（3 处导入 + `_run_check` 内的延迟 import）和 `cli.py`（2 处）。
- **踩到的真陷阱**：`agent.py` 里 `_HERE = Path(__file__).parent` 原本用来定位项目根的
  `.env` 和 `traces/`（`agent.py` 原在项目根，两者恰好重合）。搬进 `src/myagent/` 后
  `_HERE` 会变成 `src/myagent/`，`.env`/`traces/` 实际在项目根，路径错位两层——这是纯
  pytest 测试**测不出来**的 bug（测试不触发真实 API 调用/落盘），得手工排查移动后的路径语义
  才发现，改成 `_HERE.parent.parent`（上跳两级）修正。这提醒了一件事：结构性搬迁不能只看
  import 报不报错，还要检查每个"相对本文件路径"的隐含假设是否还成立。
- **测试文件同步改 import**（7 个）：`test_agent_logic.py` / `test_e2e.py` / `test_context.py` /
  `test_harness.py` / `test_subagent.py` / `test_tools.py` 加 `myagent.` 前缀；
  两个手动演示脚本 `01_run_agent.py` / `02_compaction_demo.py` 的 `sys.path.insert` 从指向
  项目根改成指向 `src/`；`00_smoke_test.py` 不 import 任何内部模块，零改动。
- **`pytest.ini`**：`pythonpath = .` → `pythonpath = src`，否则 pytest 连采集都做不到。
- **验证分两层**：① `py -m pytest -m "not e2e" -v` —— 54 绿（与改动前 P5.1 收尾时的
  53 条 + 本次未变化的条数一致，无回归）；② `cd src && py -c "import myagent, myagent.agent,
  myagent.tools, myagent.context, myagent.harness, myagent.cli"` —— 包结构自洽、无循环导入。
  **T3 自身的正式验收项**（免 `PYTHONPATH` 手动设置的裸 `py -c "import myagent"`）**留到
  T2 完成 `pip install -e .` 后一并验证**——这是 TODO.md 里已明确写好的顺序依赖，不是本次遗漏。
- **文档同步**：`CLAUDE.md` 目录 + 运行方式改用 `cd src && py -m myagent.cli`；`README.md`
  目录树和"怎么跑"同步；`TODO.md` 本次不动（等 T2 一起过验收再收尾）。

## T2 明细（快速启动 · console_scripts 入口）

- **动机**：不想每次用路径找 `main.py`；敲个 `myagent` 就进交互，对照 `claude` / `pytest` /
  `black` 这些命令行工具的同款机制。依赖 T3 已把源码收进标准包结构。
- **改动**：新增 `pyproject.toml`：
  - `[project.scripts]` 声明 `myagent = "myagent.cli:main"`（console_scripts 入口）。
  - `[tool.setuptools.packages.find] where = ["src"]`——显式告诉 setuptools 包在 `src/` 下，
    否则 src-layout 项目 setuptools 可能找不到包（自动发现默认只看项目根）。
  - `dependencies` 直接照抄 `requirements.txt` 里的三个核心依赖（anthropic/python-dotenv/
    tiktoken）；`pytest` 不列入——它是开发期工具，不是包运行时依赖，仍留在 `requirements.txt`
    给贡献者/学习者装。两份声明目前有点重复，但项目规模小，暂不引入 `[project.optional-dependencies]`
    这类分层，等依赖真的膨胀了再考虑收敛（不为不存在的问题预先设计）。
- **`py -m pip install -e .`（可编辑安装）**：把 `myagent` 装进当前 Python 环境，同时注册
  `myagent` 命令脚本（Windows 上落在 `Scripts\myagent.exe`）。`-e` 让它指回 `src/myagent/`
  源码而非拷贝一份，改代码立即生效，无需重装。
- **踩到的小陷阱**：`pip install -e .` 会在 `src/` 下生成 `myagent.egg-info/`（构建元数据，
  非源码）。补进 `.gitignore`（`*.egg-info/`），避免这个生成物被误提交。
- **验证**：① `py -c "import myagent"` 在项目根、`/tmp` 等任意目录裸执行都成功——这正是
  T3 当时明确留到本步骤才能验收的那一条，现在补上了；② `myagent` 命令在任意目录直接敲：
  banner 正常打印（模型名、4 个工具列表）、`exit` 正常退出、`PYTHONUTF8=1` 下中文不乱码；
  ③ `py -m pytest -m "not e2e" -q` 仍 54 绿，装包没有引入任何回归。
- **文档同步**：`CLAUDE.md` 运行方式改成"`pip install -e .` 后任意目录敲 `myagent`"（去掉
  T3 时写的"过渡期 `cd src && py -m myagent.cli`"说法）；`README.md` 目录树加
  `pyproject.toml` 一行、"怎么跑"改成 `myagent` 直接跑。

## T1 明细（log 开关 · MYAGENT_LOG 分级 + MYAGENT_TRACE 独立开关）

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
- **只标记 4 处为 `error`**（权限拦截 🛡️、死循环 🔁、验证门 🚧、护栏 ⛔——TODO.md 原文点名的
  "关键/错误类"），其余 11 处零改动。压缩（🗜️ 三处）判为 normal 而非 error：压缩是常规工程
  行为不是错误，这样 `off` 档"只出答案 + 错误"的语义才站得住。这就是"业务逻辑一个字不改，
  只改怎么打日志"——判断危险命令/死循环/验证失败的代码本身完全没动。
- **debug 档新增一行**：紧跟 `🧠 [Think]` 之后加 `📊 [debug]` 行，打印
  `current_context_tokens(usage) / self.compact_threshold`，让用户在 debug 档下逐轮看着
  上下文规模逼近压缩阈值，不用等压缩真触发才看到数字——呼应项目一直在做的
  KV-cache/上下文规模调查主题（见 P1/P3 明细）。
- **`_trace_enabled()`**：`MYAGENT_TRACE` 默认 `on`，`!= "off"` 即开启。`_dump_turn` 开头判断
  不满足就直接 return（不写 `turn_NN.json`）。**比 TODO.md 字面描述多做一步**：`__init__`/
  `run()` 里两处 `mkdir`（`trace_dir`/`current_task_dir`）和 `run()` 里的 `📁 [trace]`
  announce 行也一并包进同一个判断——`MYAGENT_TRACE=off` 时不仅不写文件，连空目录都不建、
  也不再打印一条指向"其实没在写"的路径，让"不生成新文件"这句验收语的精神更彻底地成立。
- **测试**：`tests/test_agent_logic.py` 新增 8 个（原 5 个纯函数测试之外）：
  - `_log` 分级 6 个（`capsys` 抓输出 + `monkeypatch.setenv`）：error 级 off 档仍打印、
    normal 级 off 档被吞、normal 级默认/normal 档正常打印、debug 级 normal 档被吞、
    debug 级 debug 档打印、非法值兜底当 normal（不报错、debug 行仍吞）。
  - `_dump_turn`/`_trace_enabled` 2 个：构造真实 `Agent()`（不调 API，`test_subagent.py` 已有
    先例）、把 `current_task_dir` 指向 `tmp_path`，验证 trace on 时写文件、off 时不写。
- **验证**：① `py -m pytest -m "not e2e" -v` —— 62 绿（原 54 + 新增 8，无回归）；
  ② 真实跑 `myagent` 三档对比：`MYAGENT_LOG=off` 下 TAOR 内部输出完全不可见、只剩 CLI 自己的
  banner/最终答案；`MYAGENT_LOG=debug` 下 Think 行后多出 `📊 [debug]` 行；`MYAGENT_LOG=off`
  + 诱导 `rm -rf` 命令，确认 `🛡️ [权限]` 拦截行依然打印（error 级不受 off 影响）；
  ③ `MYAGENT_TRACE=off` 跑一次任务，`traces/` 目录数量前后不变（65→65），且 `📁 [trace]`
  announce 行正确消失。
- **文档同步**：`CLAUDE.md` 新增"## 日志开关"小节，讲清两个变量的取值/默认/作用，以及本地
  设置的两种方式（临时前缀 vs 写进 `.env` 持久生效）；`TODO.md` T1 段落标记完成，执行顺序
  更新为 T5-A → (T4 + T5-B)。

## T5-A 明细（客制化 compact · 压缩偏好可注入 + 执行者可切换 + 主动/被动分流）

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
- **验证**：① `py -m pytest -m "not e2e"` 70 绿（62+8，无回归）；② 手动跑 `myagent`：4 步任务
  攒 10 条历史 → `/compact 只保留每个命令输出…` → `已压缩（按要求：…）：消息 10→7 条`；
  ③ **③ 子 agent 执行者手动实证**：构造 `Agent(compact_executor="subagent")`，历史里提到一个
  探针文件 → `compact_now` → 子 agent **真的启动了自己的 TAOR 循环、真的 read_file 回读核实**，
  摘要里写"（已回读 …核实，当前仍成立）"——这正是子 agent 相对盲总结的增量价值（能核实，非凭记忆）。
- **向后兼容**：不传任何 T5-A 参数时，`compact_directive=None` + `compact_executor="self"`，
  prompt 逐字等于 P3、执行者就是原 `_summarize`，行为与 P3 完全一致。

## 指令驱动压缩（轮数不足时的 /compact 降级路径）

- **起因**：实测发现会话较短时敲 `/compact 删掉后50名`，结构化压缩切不出中段、返回 None，
  回一句「轮数不足以压缩」——**用户带了明确要求也被拒**。用户是有意识地要按自己的方式压，
  此时「什么都不做」不合理。
- **最终设计（方案 C）**：新增降级路径，只接主动 `/compact`、且**必须带 directive** 才触发：
  - 保**首条** `messages[0]`（最早的目标锚点，别让 agent 忘了要干嘛）。
  - 按 directive **压其余** `messages[1:]`，压成一条摘要。压缩后 = `[首条, 摘要]`。
  - prompt 不叠加四维基础要求（`_SUMMARY_PROMPT`），完全按 directive（保头已护住锚点，放手让
    用户的要求说了算）。压缩痕迹由摘要 marker 承载（含「按要求：<directive>」，AI 后续读到即知
    这里按什么压过）。
  - **裸 `/compact`（无 directive）仍不压**：没有「用户给的方式」可依，维持现状最安全。
  - **被动压缩 `_maybe_compact` 不改**。
- **关键教训（几轮迭代才收敛）**：`/compact` 指令**根本不进历史**——[cli.py](src/contextforge/cli.py)
  拦截该命令后直接调 `compact_now` 再 `continue`，从不 append。故真实短会话（模型直答）历史就是
  `[A问题, B回答]` **两条**，想压的正是 B。中途试过「保首条 + 保末条 + 压中间」，但「保末条」在
  两条历史里恰好把要压的 B 保住了、压不动（同一条不能既保又压）；也依赖模型是否用了工具（用了才有
  ≥3 条、有中间可压），行为不稳定。方案 C **保首条 + 压其余**才真正修好：`[A,B]` → `[A, B摘要]`，
  两条直答型历史也能压。
- **`context.py`**：`compact_by_directive(messages, summarizer, directive)`（保首条 + 压 `messages[1:]`，
  门槛 `len>=2`）+ `_build_directive_only_prompt`（纯指令 prompt）。复用 `_render_middle_for_summary`，
  stats 键与 `compact_messages` 一致、上层打印不分叉。无配对风险（其余整段成摘要、保头是纯文本任务）。
- **`agent.py`**：`compact_now` 在结构化压缩返回 None **且 directive 非空**时，降级调
  `compact_by_directive`（同一个 `_pick_summarizer()`，self/subagent 执行者选择照样生效）。
- **测试**：`test_context.py`（保首压其余、两条直答型能压、纯指令不含四维、缺 directive 不压、
  不足 2 条不压）；`test_agent_logic.py`（带 directive 走降级真压且保首、裸 /compact 不压）。
- **验证**：`py -m pytest -m "not e2e"` 全绿；真实 API 复刻百家姓——`/compact 删掉后五十名`
  返回「已压缩」，`messages` 变成 `[A, 摘要]`，保住 A、把排名按要求压掉。
- **禁止补全护栏（真实 trace 驱动）+ 一条测试教训**：起初担心压缩后模型会补全被删内容——真实 trace
  里发一句 `HI`，模型果然又列出了完整前100名。**但复盘发现根因是测试输入选得不好**：`HI` 太空泛，
  被模型当成「新开场/请继续」，于是把最初「列前一百」的任务**重答**了一遍，并非「补全被删内容」。
  换成明确只需压缩后数据的问题（「排第一的是谁？只回答这一个」），模型规矩地只答「王」、一个被删名次
  都没碰——**压缩有效**。教训：验证压缩效果别用空泛输入，那会诱导模型重跑原始任务、造成「没压成」的假象。
- 护栏仍保留（摘要消息里加一行「禁止补全、恢复或重新生成被删除的部分」）：它是对「模型可能好心补全」
  的**零成本预防性声明**，写在摘要消息本身（后续每轮主 AI 都读到）。不夸大——它减弱该倾向，但模型是否
  重提旧内容更多取决于后续输入是否明确。

## T6 明细（简历前缺口补齐 · 让「机制有」变成「用户能用」）

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
- **未做的一项（明确决定不做）**：T6 原列的「偷删测试检测接线」——因简历已决定**不写**这条
  （要完全真实），故 `check_test_deletion` 保持现状（有函数有单测、未接进运行时），不强行接线。
- **测试**：`test_harness.py` +2（危险 git 命令全拦、安全 git 用法不误伤）；`test_agent_logic.py` +6
  （阈值默认/环境/显式覆盖/非法值兜底、执行者环境/显式覆盖）。
- **验证**：`py -m pytest -m "not e2e"` 81 绿（原 73 + 8，无回归）；代码层实证 harness 硬拦 4 个
  危险 git 命令、放行 4 个安全 git 用法——「用代码强制、不靠模型自觉」（模型碰巧自己拒了也不算数，
  harness 那道关照挡）。
- **文档**：`.env` 加 `MYAGENT_COMPACT_THRESHOLD` / `MYAGENT_COMPACT_EXECUTOR` 注释示例；
  `CLAUDE.md` 补这两个环境变量说明。

## 验证门用户入口（环境变量 + /check · 承 T6「让机制有变成用户能用」）

- **起因**：更名后重读验证门，发现它和 T6 修过的那批是同类漏——`ValidationGate` 逻辑早已接入 TAOR
  循环（声称完成→跑检查→失败打回），但 `check_command` **只有构造参数入口**，CLI 两处都是裸
  `Agent()`。结果：从 `contextforge`/`cf` 跑的用户根本配不了，验证门永远走「未配置→跳过」，形同虚设。
  compact 三兄弟（directive/threshold/executor）都补过环境入口，唯独 `check_command` 漏了。
- **① 环境变量入口** `CONTEXTFORGE_CHECK_COMMAND`：`Agent.__init__` 加兜底「显式 > 环境 > None」，
  与 `compact_directive` 同款。解析后存一份到 `self.check_command`（供 CLI 查看/复用），再建门。
- **② CLI `/check` 命令**：`/check <命令>` 当场设、空 `/check` 查看、`/check off` 清除。设/清都
  **重建 `ValidationGate`**（门的 check_command 是构造时定的只读字段，不给 harness 加 setter）。
  命令存在 Agent 实例上，`reset` 重建实例即清空、回到环境变量默认——语义一致（reset = 彻底重来）。
- **测试**：`test_agent_logic.py` +3（环境兜底且同步进门、显式覆盖环境、默认 None 时门无条件放行）。
- **验证**：`py -m pytest -m "not e2e"` 85 绿（原 82 + 3，无回归）；环境变量入口实测
  `check_command` 与门内值一致；CLI `/check` 设/查/清四步交互全通、`reset` 清掉确认。
- **文档**：`.env` 加注释示例；`CLAUDE.md` 环境变量表 + CLI 命令段补 `/check`；`README.md` 会话内命令补一行。

## P0 明细

- [x] 建 `C:\AI_learning\myagent`（与学习仓库平级）
- [x] `requirements.txt`（anthropic + tiktoken）
- [x] `.gitignore`
- [x] `CLAUDE.md`（项目宪法）
- [x] `PROGRESS.md`（本文件）
- [x] `tests/00_smoke_test.py` 跑通一次 Anthropic 调用（返回 pong）

## P1 明细

- [x] `tools.py`：工具层（read_file / run_command + 手写 schema + execute_tool 分发）
- [x] `agent.py`：核心 TAOR 循环（Think→Act→Observe→Repeat）+ 每轮 trace/log + max_iterations 护栏
- [x] `tests/01_run_agent.py`：两步任务跑通，完整 TAOR 轨迹可见
- [x] `main.py`：正式交互式 CLI 入口（`py main.py`，输入任务 / reset / exit）
- [x] **Token 调查 trace**：每轮把「实际发出的 messages + usage 4 字段」落盘到
      `traces/<年>/<月>/<日>/run_<时分秒>/task_NN/turn_NN.json`（按日期分层归档），
      循环里同步打印 `in=.. (cache_read=.., cache_write=..)`。
- **KV Cache 实地验证结论**（用户亲自调查）：`input_tokens` 只是「按全价算的部分」，
      不等于发出去的总量。turn_02 的 `messages_sent` 有 3 条（含 turn_01 全部内容），
      实际 1958 token 全进了 `cache_creation_input_tokens`，故 `input_tokens` 只剩 2。
      **每轮都发完整历史**得证。发出去总量 = input + cache_creation + cache_read。
- **观察**：模型第一轮就**并行**请求了 run_command + read_file 两个工具（判断无依赖）。
  我们的 `for block in response.content` 遍历天然支持多工具 —— 并行工具调用提前出现，
  Phase 2 会把「并行**执行**」正式补上（当前是顺序执行多个工具）。

## 决策记录（与初版计划的差异）

- **不建虚拟环境**：直接用系统 Python 3.11（`py` 启动器）。ping pong 已用系统 Python 验证通路可用。
- **凭据改用本地 `.env`**（原计划想省掉）：那套 `ANTHROPIC_AUTH_TOKEN` / `BASE_URL` / `MODEL`
  是 VSCode 扩展**只注入给它派生的子进程**（Claude Code 工具），**普通集成终端拿不到**
  （注册表用户级环境变量里也没有）。故落到 `myagent/.env`（`.gitignore` 已挡，不进 git/Sync），
  代码用 `python-dotenv` 的 `load_dotenv()` 加载。`terminal.integrated.env.windows` 试过能注入
  非敏感值，但 token 不宜写进全局 settings，最终选 `.env`。
- **模型 ID 从环境读**，不写死。当前为 `claude-opus-4-8[1m]`。
- **myagent 位置**：`C:\AI_learning\myagent`（平级），非仓库内。
- **代理端口**：`http://127.0.0.1:23333/api/anthropic`，VSCode 开着时才在（本地服务）。

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
- 文档：README / CLAUDE / HIGHLIGHTS 全改为新名；本文件与 TODO 顶部加更名说明，**历史明细里
  P1–P5、T 系列出现的 `myagent` / `MYAGENT_` 作为「当时的名字」保留不改**（改了反而失真）。

**不用改**：`_HERE = .parent.parent.parent`（`src/contextforge/` → `src/` → 根，深度没变）；
物理仓库文件夹仍是 `C:\AI_learning\myagent`（只搬了包目录，没重命名外层文件夹）。

**验证**：`py -m pytest -m "not e2e"` 全绿（82）；`cf` / `contextforge` 两命令均可启动；
`grep -rn "myagent\|MYAGENT_" src/ tests/ pyproject.toml` 代码/配置零残留（仅历史文档叙述保留旧名）。
