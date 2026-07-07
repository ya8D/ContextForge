# TODO —— myagent 后续计划

> 本文件记「计划要做的」。已做完的详见 [PROGRESS.md](./PROGRESS.md)，稳定约定见 [CLAUDE.md](./CLAUDE.md)。
> 核心五阶段（P1-P5）+ 两次 review 修正（P4.1 / P5.1）已完成。以下是四个后续 TODO。

## 执行顺序

建议 **T3 → T2 → T1 → T4**：先挪包（改动最大、其余都依赖新结构），再加入口，再加 log 开关，最后写文档。
每步跑测试确认不退化；全部做完再一起 push（commit 由 AI，push 由用户本人）。

---

## T3 — 标准包 src 布局（先做，改动最大）

**为什么**：项目要变成可安装的命令行工具（见 T2），需规范成标准 Python 包。

**做什么**：
- 源码收进 `src/myagent/`：`agent.py` / `tools.py` / `context.py` / `harness.py` / `cli.py`（原 `main.py`）+ `__init__.py`。
- 所有 import 从 `from tools import ...` 改成 `from myagent.tools import ...`（**所有源码 + 所有测试**）。
- 顶层保留 `pyproject.toml` / `README.md` / `tests/` / `docs/`。

**风险**：59 个测试的 import 全要改，`git diff` 会很大（几乎每个文件都动）。一步步来、每步跑测试。

**验证**：`py -m pytest -m "not e2e"` 全绿；`py -c "import myagent"` 不炸。

---

## T2 — 快速启动（像 `claude` 一样敲命令就开）

**为什么**：不想每次用路径找 `main.py`；敲个 `myagent` 就进交互。

**做什么**：
- 加 `pyproject.toml`，声明 console_scripts 入口：`myagent = "myagent.cli:main"`。
- `py -m pip install -e .`（可编辑安装）后，**任何目录**敲 `myagent` 启动交互 CLI。
- `-e` 可编辑模式：改代码立即生效，无需重装。

**对照**：这正是 `claude` / `pytest` / `black` 这些命令行工具的同款机制。

**验证**：任意目录敲 `myagent` → 进入「你的任务>」交互。

---

## T1 — log 开关（环境变量，分级 + 双开关）

**为什么**：让用户能开关日志——平时安静，调试时详细。

**设计**：
- **`MYAGENT_LOG=off / normal / debug`** 控**屏幕**输出：
  - `off`：只出最终答案 + 错误
  - `normal`（默认）：出 TAOR 每轮关键行（现在的样子）
  - `debug`：再加细节（完整参数、每轮 usage 明细等）
- **`MYAGENT_TRACE=on / off`** 独立控 **`traces/` 落盘**（默认 on）。
  - 两个独立开关：可以「屏幕安静但文件还留着」（事后调查用）。

**实现要点**：
- 只在 `_log` 函数**内部**读一次 flag 决定打不打；**调用点一个字不改**（当初把 log 收敛到一个 `_log` 的好处）。
- `_dump_turn` 内部加 `MYAGENT_TRACE` 判断。
- 环境变量带 `MYAGENT_` 前缀，不与别的软件撞。
- **不引入标准 `logging`**：它会吃掉我们精心设计的 emoji 前缀/排版；保留自己的 `_log` + 简单分级，贴合项目「手搓、可读」气质。

**验证**：`MYAGENT_LOG=off py -m ...` 只见答案；`normal` 见每轮；`MYAGENT_TRACE=off` 时 `traces/` 不生成新文件。

---

## T4 — 学习笔记（`docs/`，主题式叙事，标题即结论）

**为什么**：把这一路学到的**认知性经验**沉淀下来，**给人 & AI 都看**（不是给 AI 的运行记录、也不是排错手册）。

**形式**：主题式叙事，每篇讲透一个「学到了什么」。结构统一：
**问题是什么 → 我原来怎么想 → 真相 / 原理 → 学到的道理**。
读起来像学习心得，标题一眼见结论。

**篇目**（`docs/` 下）：
- `01-kv-cache-每轮全量历史为何不贵.md` —— in=2 的真相：input 只是未缓存新增量，其余命中缓存
- `02-上下文压缩不是截断API而是本地重写历史.md` —— API 无状态，压缩 = 我们在本地重写 messages
- `03-harness-用代码强制而非靠模型自律.md` —— 约束卡在「请求 → 执行」之间，不靠模型自觉
- `04-sub-agent-本质是一个工具.md` —— 决策交给模型、执行和隔离留给代码
- `05-死循环检测-reset反而放过真循环.md` —— 清零机制看似合理，实则纵容不听劝的循环
- `06-循环import是东西放错了层的信号.md` —— 延迟 import 是治标，根治是把东西挪到正确的层
- 末尾感悟（贯穿 05/06）：**人 review + 测试互补**——测试保证「不退化」，人审发现「设计对不对」

**验证**：`docs/` 下 6 篇 + 每篇标题一眼见结论；人读顺畅、AI 读能懂设计哲学。

---

## 备注

- **commit / push 分工**：commit 由 AI 完成（本地、可逆）；**push 必须由用户本人执行**（外发 GitHub 由用户把关）。
- 每个 TODO 收尾 = 实现 + 测试跑绿 + 更新 PROGRESS.md（项目固定规矩）。
