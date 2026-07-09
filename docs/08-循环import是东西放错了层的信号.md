# 08 · 循环 import 是「东西放错了层」的信号

## 问题是什么

`spawn_subagent`（派生子 agent 的工具）该放哪个文件？它是个 `@tool`，直觉上所有工具都在
`tools.py`，那它也该在 `tools.py`。可它内部要 `new` 一个 `Agent`——于是 `tools.py` 得
`from agent import Agent`。而 `agent.py` 顶部本来就有 `from tools import ...`。两条 import 一撞：
**循环 import**，程序起不来。

## 我原来怎么想

遇到循环 import，第一反应是找「怎么绕过去」。Python 有现成的招：**延迟 import**——把 `from agent
import Agent` 从文件顶部挪进函数体内部，用到时才导：

```python
# tools.py（当时的想法）
def spawn_subagent(task):
    from agent import Agent    # 延迟到函数内部，避开顶部的循环
    ...
```

这确实能让程序跑起来。我一度觉得问题解决了——报错没了嘛。

## 真相 / 原理

延迟 import **能让报错消失，但它是治标**。循环 import 报错不是 Python 在刁难你，它是在**报告一个
真实的设计问题：你把东西放错了层**。

想清楚依赖该往哪个方向流：

- `agent.py` 依赖 `tools.py`（agent 要用工具）——这是**顺流**，正确。
- 如果 `tools.py` 又依赖 `agent.py`（工具要用 Agent）——这是**逆流**，制造了环。

`spawn_subagent` 的本质是"**一个关于 Agent 的工具**"——它离不开 `Agent`。所以它根本不属于
`tools.py`（那是"不依赖 Agent 的基础工具"该待的层），它属于 `agent.py`。把它挪到 `agent.py`
（`Agent` 定义之后），逆流边**彻底消失**，依赖单向朝下：

```python
# agent.py：spawn_subagent 定义在这里，Agent 定义之后
# 它直接用同文件的 Agent，无需任何跨模块 import —— 环从根上没了。
@tool({"task": "..."})
def spawn_subagent(task: str) -> str:
    sub = Agent(tools=subagent_tool_schemas(), max_iterations=15)  # 直接用同文件的 Agent
    return f"[子 agent 完成] {sub.run(task)}"
```

`@tool` 装饰器仍来自 `tools`（顺流，没问题），执行时把这个函数注册进 `tools.TOOL_SCHEMAS`。

有个优雅的副产品：现在**"有没有 spawn_subagent 这个工具"取决于有没有 import `agent`**——
干活工具（read/run/write）导 `tools` 就注册；这个"关于 Agent 的工具"导 `agent` 才注册。语义上
恰好正确：**不 import agent，就没有"派生 agent"的能力**。放对了层，连这种一致性都是免费的。

## 学到的道理

- **循环 import 不是要"绕过"的障碍，是要"听懂"的信号**。它在说："这两个模块的依赖成环了，
  其中有东西放错了层。" 延迟 import 把信号消音了，问题还在。
- **治标 vs 根治**：延迟 import（挪进函数体）是治标——环还在，只是错开了触发时机。根治是**把东西
  挪到依赖正确的层**，让依赖单向流动、环从根上不存在。
- **判断"该放哪一层"，看它依赖谁**。`spawn_subagent` 依赖 `Agent`，就该和 `Agent` 同层或更上层，
  不能塞进"本该不依赖 Agent"的 `tools.py`。**依赖方向决定归属**。
- **放对位置后，正确性会自己涌现**。工具的可用性恰好绑定到"是否 import 了对应模块"，这种语义一致
  不是我刻意设计的，是"东西在对的层"自然带出来的。**结构对了，很多细节不用操心。**
- 这和 [06 · 死循环 reset](./06-死循环检测-reset反而放过真循环.md) 是一类教训：**图省事的"快解"
  （延迟 import / 命中后 reset）往往只是把问题藏起来**，真正的解要回到问题的根上。
