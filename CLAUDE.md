# myagent — 项目宪法（CLAUDE.md）

> 本文件是项目的**稳定约定**。易变的进度见 [PROGRESS.md](./PROGRESS.md)，两者分开维护。

## 这是什么

一个**从零手搓**的 agent / harness 学习项目：亲手实现 agent loop、tool use、
上下文管理、harness 约束、sub-agent 等机制。目标是**理解原理**，不是造一个通用框架。
配套教材是隔壁的 `agent_learning`（mdBook，正文在 `src/zh/`），本项目边搭边对照其章节。

## 硬约定

- **语言**：所有交流、注释、文档一律用**中文**。
- **模型 API**：Anthropic (Claude) Python SDK。
- **不用框架**：不引入 LangChain / LangGraph，编排逻辑全部自己写。
- **模型 ID 从环境读，绝不写死**：用 `os.environ.get("ANTHROPIC_MODEL")`。
  当前环境由 VSCode Copilot 注入以下变量，SDK 自动读取，**无需 `.env`**：
  - `ANTHROPIC_AUTH_TOKEN`（鉴权）
  - `ANTHROPIC_BASE_URL`（本地代理地址）
  - `ANTHROPIC_MODEL`（如 `claude-opus-4-8[1m]`）

## 运行方式

- Python：**系统 Python 3.11**，用 `py` 启动器调用（bash 里 `python` / `python3` 不通）。
- 不建虚拟环境，依赖装在全局。安装：`py -m pip install -r requirements.txt`
- 运行任意脚本：`py <脚本名>.py`
- 冒烟测试（验证 API 通路）：`py tests/00_smoke_test.py`
- **自动化测试**：`py -m pytest -m "not e2e"`（纯逻辑，不烧钱）；`py -m pytest -m e2e`（真调 API）。
- 中文输出：跑脚本时加 `PYTHONUTF8=1`，避免终端 GBK 乱码。

## 目录

```
myagent/
  requirements.txt      # anthropic + tiktoken
  tests/
    00_smoke_test.py    # 最小 API 调用验证
  agent.py              # 核心 TAOR loop（后续阶段逐步长大）
  tools.py              # 工具注册表 + 内置工具（含 P5 spawn_subagent 派生子 agent）
  context.py            # 上下文/压缩（P3：真实 usage 判规模，超阈值压中段）
  harness.py            # 权限拦截 + 死循环检测 + 验证门（P4：三根柱子）
  main.py               # CLI 入口
```
（除 requirements/smoke 外，其余文件随对应 Phase 逐步创建。）

## 编码风格

- 面向学习：关键处写清「为什么这么做」的中文注释，宁可啰嗦。
- 与教材对照：实现某机制时，注释里标注对应 `agent_learning` 章节。
- **术语统一**：agent 主循环一律叫 **TAOR**（Think → Act → Observe → Repeat）。
- **可观测**：TAOR 循环内置 trace/log，每轮打印 Think / Act / Observe，让循环透明可见
  （对照 agent_learning 第 18.5 节 可观测性）。测试统一放 `tests/`。
