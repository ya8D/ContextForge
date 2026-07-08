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

## 日志开关（T1）

两个独立的环境变量，互不影响：

| 变量 | 取值 | 默认 | 作用 |
|---|---|---|---|
| `MYAGENT_LOG` | `off` / `normal` / `debug` | `normal` | 控**屏幕**输出：`off` 只出最终答案 + 权限拦截/死循环/验证门/护栏这类错误；`normal` 是现在的样子（TAOR 每轮 Think/Act/Observe）；`debug` 再加逐轮上下文规模等细节。 |
| `MYAGENT_TRACE` | `on` / `off` | `on` | 独立控 `traces/` 落盘。可以「屏幕安静但文件还留着」，反之亦可。 |

本地怎么设置：
- **临时单次**（当前终端是 git-bash，前缀语法直接可用）：
  ```bash
  MYAGENT_LOG=debug myagent
  MYAGENT_LOG=off MYAGENT_TRACE=off myagent
  ```
- **持久默认**：写进 `myagent/.env`（已 gitignore，`load_dotenv` 自动加载）一行，比如
  `MYAGENT_LOG=debug`，之后每次敲 `myagent` 都生效，无需再加前缀。

## 运行方式

- Python：**系统 Python 3.11**，用 `py` 启动器调用（bash 里 `python` / `python3` 不通）。
- 不建虚拟环境，依赖装在全局。安装：`py -m pip install -r requirements.txt`
- **可编辑安装**（T2 起）：`py -m pip install -e .` 后，**任意目录**敲 `myagent` 即可启动交互
  CLI（`pyproject.toml` 声明的 console_scripts 入口）。`-e` 可编辑模式：改代码立即生效，
  无需重装。
- **CLI 交互命令**：`exit`/`quit`/`q` 退出；`reset` 清空记忆开新会话；
  `/compact [要求]` 手动压缩历史（T5-A）——可跟一段话指定保留/删除什么
  （如 `/compact 只保留登录相关报错，其余删掉`），不跟则按默认/会话级偏好压。
- 运行 `tests/` 下的手动演示脚本：`py tests/<脚本名>.py`（脚本内部已把 `src/` 插入
  `sys.path`，可直接跑，无需切目录）。
- 冒烟测试（验证 API 通路）：`py tests/00_smoke_test.py`
- **自动化测试**：`py -m pytest -m "not e2e"`（纯逻辑，不烧钱）；`py -m pytest -m e2e`（真调 API）。
- 中文输出：跑脚本时加 `PYTHONUTF8=1`，避免终端 GBK 乱码。

## 目录

```
myagent/
  pyproject.toml         # console_scripts 入口（myagent = myagent.cli:main）+ src 布局声明
  requirements.txt      # anthropic + tiktoken
  pytest.ini            # pythonpath = src，让 tests/ 能 import src/myagent 包
  src/
    myagent/
      __init__.py       # 包标记
      agent.py          # 核心 TAOR loop（后续阶段逐步长大）
      tools.py          # 工具注册表 + 内置工具（含 P5 spawn_subagent 派生子 agent）
      context.py        # 上下文/压缩（P3：真实 usage 判规模，超阈值压中段）
      harness.py        # 权限拦截 + 死循环检测 + 验证门（P4：三根柱子）
      cli.py             # CLI 入口（原 main.py，T3 标准包布局后改名）
  tests/
    00_smoke_test.py    # 最小 API 调用验证
```
（除 requirements/smoke 外，其余文件随对应 Phase 逐步创建。T3 起源码收进
`src/myagent/`，标准 Python 包布局；T2 起 `pip install -e .` 后任意目录可直接敲 `myagent` 启动。）

## 编码风格

- 面向学习：关键处写清「为什么这么做」的中文注释，宁可啰嗦。
- 与教材对照：实现某机制时，注释里标注对应 `agent_learning` 章节。
- **术语统一**：agent 主循环一律叫 **TAOR**（Think → Act → Observe → Repeat）。
- **可观测**：TAOR 循环内置 trace/log，每轮打印 Think / Act / Observe，让循环透明可见
  （对照 agent_learning 第 18.5 节 可观测性）。测试统一放 `tests/`。
