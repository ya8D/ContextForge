# ContextForge —— 面向超大型代码库的可控编码 Agent

一个**从零手写、不用框架**（不依赖 LangChain / LangGraph）的 AI 编码 agent，用
**Anthropic (Claude) SDK** 把 agent 的每一层机制亲手实现一遍，并针对超大型项目（如 Chromium）
的真实痛点做了优化——特色是**可客制化的上下文压缩**（内容 + 触发阈值双维度可调）、
**代码强制的安全约束**（含危险 git 命令拦截），以及可观察的
**Coordinator → 并行 Workers → Reviewer 多 Agent 协作 Demo**。

配套教材是隔壁的 mdBook `agent_learning`，本项目边搭边对照其章节。

## 这个 agent 有什么

从最裸的 while 循环，一层层加到五脏俱全：

| 阶段 | 能力 | 核心文件 |
|---|---|---|
| **P1 裸 Agent Loop（TAOR）** | agent 的本质：Think → Act → Observe → Repeat（带工具的 while 循环） | `agent.py` |
| **P2 多工具 + 并行 + 回喂截断** | `@tool` 装饰器自动生成 schema；一轮多工具并行执行；先读再改约束 | `tools.py` |
| **P3 上下文压缩** | 每轮发送前用 Anthropic Token Counting 预检；超软阈值时保任务入口/第一轮/近 3 轮、压真正中段；硬超限时有界阻止 | `agent.py` + `context.py` |
| **P4 Harness 三根柱子** | 权限拦截（危险命令/路径遍历）、死循环检测、验证门（防假完成） | `harness.py` |
| **P5 Sub-agent** | 主 agent 派生**上下文隔离**的子 agent 跑子任务、只回传结论 | `agent.py`（`spawn_subagent`） |
| **多 Agent 协作 Demo** | Coordinator 结构化拆分 → 2～4 个只读 Worker 有界并行 → Reviewer 回读证据 → 一次定向补充 → 独立 Aggregator 汇总 | `collaboration.py` |

## 关键设计原则

- **Agent Loop（TAOR）**：业界交流称 Agent Loop；本项目按 Think（调 LLM 决策）→ Act（执行工具）→
  Observe（回喂结果）→ Repeat 将四阶段简称为 TAOR。它与 ReAct（Reasoning + Acting）思想同源，
  但这里强调的是 Harness 真正执行的循环结构，而非把二者当成完全相同的术语。
- **请求前精确预检**：每轮用与实际请求一致的 model/messages/system/tools 调 `messages.count_tokens`；
  软阈值负责质量/成本压缩，显式 `CONTEXTFORGE_MAX_INPUT_TOKENS` 负责硬准入，估算值不混入计费 usage。
- **保护任务入口与第一轮**：最近一次 `run(task)` 的完整用户原文、全会话最早入口，以及各自第一完整轮都不进摘要/清理区；
  语义压缩失败只清旧工具结果正文，不自动删除任何完整轮，仍超硬预算就停止发送。
- **每轮重发最终历史**：API 无状态，KV Cache 让重发的部分便宜（`input + cache_read + cache_write` = 真实发出总量）。
- **用代码强制，不靠模型自律**：harness 约束（权限、循环、验证）都卡在「模型请求 → 真正执行」之间。
- **决策交给模型，执行和约束留给代码**：单个子 agent 何时派由模型判断；团队协作中的并发、协议校验和
  最多一次定向补充由代码控制，避免模型自己决定控制流。
- **可观测**：Agent Loop（TAOR）每轮打印 Think / Act / Observe，并把 in/out 落盘到 `traces/`；团队另有 `team.json`
  保存结构化业务交接，并以角色、attempt、耗时、usage 和 `trace_ref` 串起各 Agent 的逐轮 trace（均已 gitignore）。

> 📌 想看这个项目**哪里特别、为什么这么设计**（都是实地验证过/踩过的亮点），见 [HIGHLIGHTS.md](./HIGHLIGHTS.md)。

## 目录

```
pyproject.toml # console_scripts 入口（contextforge / cf 命令）+ src 布局声明
src/contextforge/
  agent.py     # 核心 Agent Loop（TAOR）+ spawn_subagent + 结构化 AgentRunResult
  collaboration.py # Coordinator → 并行 Workers → Reviewer 协作控制面
  tools.py     # 全局 @tool + 实例级 LocalTool + 基础工具
  context.py   # 上下文压缩（P3）
  harness.py   # 权限拦截 + 死循环检测 + 验证门（P4）
  cli.py       # 交互式 CLI 入口（原 main.py，T3 标准包布局后改名）
tests/         # pytest：纯逻辑（不烧钱）+ 端到端（真调 API，标 @pytest.mark.e2e）
HIGHLIGHTS.md  # 设计亮点速览（哪里特别 / 为什么这么设计，都验证过）
PROGRESS.md    # 逐阶段的详细开发记录（含每个决策的「为什么」）
CLAUDE.md      # 项目稳定约定（选型 / 语言 / 运行方式）
```

## 怎么跑

> 需要 Anthropic API 凭据。本项目从环境变量读（`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` /
> `ANTHROPIC_MODEL`），放在本地 `.env`（**已 gitignore，绝不进库**）。这三项以项目 `.env` 为准，
> 会覆盖 IDE/父进程注入的同名值；`CONTEXTFORGE_LOG` 等临时运行开关不受影响。

```bash
py -m pip install -r requirements.txt      # 装依赖（anthropic + pytest 等）
py -m pip install -e .                      # 可编辑安装，注册 contextforge / cf 命令

contextforge                                # 任意目录敲命令即可交互（或短别名 cf）：输入任务 / reset / exit
                                            # /compact、/check，以及 /team <只读协作分析目标>

py -m pytest -m "not e2e"                   # 跑纯逻辑测试（毫秒级，不烧钱）
py -m pytest                                # 跑全部（含真调 API 的 e2e）
```

> Windows 用 `py` 启动器（bash 里 `python`/`python3` 可能不通）。中文输出加 `PYTHONUTF8=1` 防乱码。

## 说明

这是**学习项目**：关键处写清「为什么这么做」的中文注释，`PROGRESS.md` 记录了每个阶段的设计推敲
与踩过的坑（包括逐行 review 揪出的两个真 bug）。不追求生产级完备，追求「每一层都懂原理」。
