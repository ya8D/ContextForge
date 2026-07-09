"""
test_subagent.py —— P5 Sub-agent 的纯逻辑测试（不调 API，不烧钱）。

真正"派生子 agent 并跑通"要调 API，放 e2e。这里测**上下文隔离机制本身**：
两个 Agent 实例的对话历史是彼此独立的对象；子 agent 拿到的是受限工具集。
这些都不需要调 API —— 只验证对象结构，毫秒级。

跑法：py -m pytest tests/test_subagent.py -v （属于 "not e2e" 那批）
"""

from contextforge.agent import Agent
from contextforge.tools import TOOL_SCHEMAS, subagent_tool_schemas


def test_importing_agent_registers_spawn_subagent():
    """P5.1 注册时机：spawn_subagent 定义在 agent.py，import agent 后它才注册进 TOOL_SCHEMAS。

    本文件顶部已 `from agent import Agent`，所以 agent 模块已加载、装饰器已执行。
    钉死这个新的注册时机：解循环导入后，spawn_subagent 归 agent.py 定义，
    只有导入 agent 才把它注册进 tools.TOOL_SCHEMAS。
    """
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert "spawn_subagent" in names


def test_two_agents_have_isolated_messages():
    """上下文隔离的根基：两个 Agent 实例的 messages 是不同对象。

    子 agent 往自己 messages 堆东西，不会碰到主 agent 的 messages —— 因为它们是
    两个独立的 list 对象。这是"子 agent 噪声不回灌主 agent"的物理保证。
    """
    main_agent = Agent()
    sub_agent = Agent(tools=subagent_tool_schemas())
    # 两个 messages 是不同对象（is 比较对象身份，不是内容）。
    assert main_agent.messages is not sub_agent.messages
    # 往子 agent 的 messages 加东西，主 agent 的不受影响。
    sub_agent.messages.append({"role": "user", "content": "子任务"})
    assert len(main_agent.messages) == 0
    assert len(sub_agent.messages) == 1


def test_main_agent_has_all_tools():
    """主 agent（默认）拿到全集工具，含 spawn_subagent。"""
    main_agent = Agent()
    names = {t["name"] for t in main_agent.tool_schemas}
    assert "spawn_subagent" in names


def test_sub_agent_has_restricted_tools():
    """子 agent 拿到受限工具集，不含 spawn_subagent（防无限递归派生）。"""
    sub_agent = Agent(tools=subagent_tool_schemas())
    names = {t["name"] for t in sub_agent.tool_schemas}
    assert "spawn_subagent" not in names
    # 但基础工具在
    assert {"read_file", "run_command", "write_file"} <= names


def test_default_tools_is_full_set():
    """不传 tools 时默认用全局全集（主 agent 行为不变，向后兼容）。"""
    agent = Agent()
    assert agent.tool_schemas is TOOL_SCHEMAS


def test_subagent_uses_smaller_iteration_cap():
    """子 agent 该有更小的 max_iterations（子任务不该跑那么多轮，防失控）。

    这里只验证「能传入更小的上限」这个能力；spawn_subagent 工具内部用的是 15。
    """
    sub = Agent(tools=subagent_tool_schemas(), max_iterations=15)
    assert sub.max_iterations == 15
