"""请求前 Token Counting 与保守压缩兜底测试（不调真实 API）。"""

import copy

import pytest

from contextforge.agent import Agent
from contextforge.context import (
    compact_messages_protected,
    micro_compact_tool_results,
)
from contextforge.tools import LocalTool, tool_success


class _Count:
    def __init__(self, input_tokens):
        self.input_tokens = input_tokens


class _Usage:
    input_tokens = 11
    output_tokens = 7
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Block:
    def __init__(self, block_type, **kwargs):
        self.type = block_type
        self.__dict__.update(kwargs)

    def model_dump(self):
        return {"type": self.type, **self.__dict__}


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()
        self.model = "RESPONSE_MODEL"


def _turn(index, *, result_size=40):
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": f"第{index}轮分析 FIRST_SENTINEL" if index == 0 else f"第{index}轮分析",
                },
                {
                    "type": "tool_use",
                    "id": f"tool-{index}",
                    "name": "probe",
                    "input": {"index": index},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"tool-{index}",
                    "content": f"RESULT_{index}_" + "x" * result_size,
                }
            ],
        },
    ]


def _current_task_history(turns=5, *, result_size=40):
    anchor = {"role": "user", "content": "CURRENT_TASK_ANCHOR：完整保留这句话"}
    messages = [anchor]
    for index in range(turns):
        messages.extend(_turn(index, result_size=result_size))
    return messages, anchor


def _assert_adjacent_tool_pairs(messages):
    """逐条验证 Anthropic 要求的紧邻配对，集合相等不足以证明消息合法。"""
    result_occurrences = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_id = block["tool_use_id"]
                result_occurrences[tool_id] = result_occurrences.get(tool_id, 0) + 1

    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        use_ids = []
        for block in message["content"]:
            block_type = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if block_type == "tool_use":
                use_ids.append(getattr(block, "id", None) or block["id"])
        if not use_ids:
            continue
        assert index + 1 < len(messages), f"tool_use {use_ids} 后没有 user(tool_result)"
        next_message = messages[index + 1]
        assert next_message.get("role") == "user" and isinstance(next_message.get("content"), list)
        adjacent = {
            block["tool_use_id"]
            for block in next_message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        }
        assert set(use_ids) <= adjacent
        assert all(result_occurrences[tool_id] == 1 for tool_id in use_ids)


def _tool_pair_ids(messages):
    uses = set()
    results = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            block_type = getattr(block, "type", None) or block.get("type")
            if block_type == "tool_use":
                uses.add(getattr(block, "id", None) or block["id"])
            if block_type == "tool_result":
                results.add(block["tool_use_id"])
    return uses, results


def test_protected_semantic_compaction_keeps_current_anchor_and_first_turn():
    messages, anchor = _current_task_history()
    original = copy.deepcopy(messages)
    captured = {}

    def summarize(prompt):
        captured["prompt"] = prompt
        return "压缩后的真正中段"

    compacted, stats = compact_messages_protected(
        messages,
        summarizer=summarize,
        task_anchor=anchor,
        keep_recent_turns=3,
    )

    assert stats is not None
    assert compacted[0] is anchor
    assert compacted[0]["content"] == "CURRENT_TASK_ANCHOR：完整保留这句话"
    assert compacted[1] is messages[1] and compacted[2] is messages[2], (
        "当前任务第一完整轮必须以原对象保留，不得进入摘要后再伪造等值副本"
    )
    assert compacted[1:3] == original[1:3], "当前任务第一完整轮不得进入摘要区"
    assert "FIRST_SENTINEL" in str(compacted[1:3])
    assert "FIRST_SENTINEL" not in captured["prompt"], "第一完整轮内容不得发送给摘要执行者"
    assert compacted[-6:] == original[-6:], "最近 3 个完整轮必须原文保留"
    assert "前情摘要" in compacted[3]["content"]
    assert _tool_pair_ids(compacted)[0] == _tool_pair_ids(compacted)[1]
    _assert_adjacent_tool_pairs(compacted)
    assert messages == original, "候选生成不得原地修改历史"


def test_protected_compaction_uses_latest_run_anchor_in_multi_task_history():
    old_history = [
        {"role": "user", "content": "旧任务原文也要进入语义摘要"},
        {"role": "assistant", "content": "旧任务回答"},
    ]
    current, anchor = _current_task_history()
    messages = [*old_history, *current]
    captured = {}

    compacted, stats = compact_messages_protected(
        messages,
        summarizer=lambda prompt: captured.setdefault("prompt", prompt) and "跨任务摘要",
        task_anchor=anchor,
        keep_recent_turns=3,
    )

    assert stats is not None
    assert compacted[0:2] == old_history, "全会话最早任务入口与第一轮也必须原文保留"
    assert compacted[2] is anchor, "当前任务入口必须按对象边界识别，而不是误取 messages[0]"
    assert compacted[2]["content"] == "CURRENT_TASK_ANCHOR：完整保留这句话"
    assert compacted[3:5] == current[1:3], "当前任务第一完整轮必须原文保留"
    assert "前情摘要" in compacted[5]["content"], "当前任务中段摘要必须位于当前入口之后"
    assert "旧任务原文" not in captured["prompt"], "最早任务入口与第一轮不得进入摘要区"


def test_micro_compaction_only_clears_eligible_tool_results_and_deletes_no_turns():
    messages, anchor = _current_task_history(result_size=200)
    original = copy.deepcopy(messages)

    compacted, stats = micro_compact_tool_results(
        messages,
        task_anchor=anchor,
        keep_recent_turns=3,
    )

    assert stats is not None
    assert len(compacted) == len(original), "micro-compaction 不得删除任何完整轮"
    assert compacted[0] is anchor
    assert compacted[1:3] == original[1:3], "第一完整轮必须逐块原文保留"
    assert compacted[-6:] == original[-6:], "最近 3 轮必须原文保留"
    # 5 轮时只有第 2 轮（index=1）位于“第一轮”和“最近3轮”之间，只有它的工具正文可清理。
    assert "旧工具结果正文已清理" in compacted[4]["content"][0]["content"]
    assert "RESULT_1_" not in compacted[4]["content"][0]["content"]
    assert "第1轮分析" in str(compacted[3]), "assistant 分析文本不得被清理"
    assert _tool_pair_ids(compacted)[0] == _tool_pair_ids(compacted)[1]
    _assert_adjacent_tool_pairs(compacted)
    assert messages == original


def test_preflight_counts_exact_request_before_create(monkeypatch):
    events = []
    counted = []
    created = []
    local = LocalTool(
        name="schema_probe",
        description="证明工具 schema 同时进入 count 与 create。",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda data: tool_success("ok"),
    )
    agent = Agent(
        model="REQUEST_MODEL",
        tools=[],
        local_tools=[local],
        system_prompt="SYSTEM_SENTINEL",
        compact_threshold=999_999,
        max_input_tokens=20_000,
        max_tokens=100,
        check_command=None,
    )

    def fake_count(**kwargs):
        events.append("count")
        counted.append(copy.deepcopy(kwargs))
        return _Count(123)

    def fake_create(**kwargs):
        events.append("create")
        created.append(copy.deepcopy(kwargs))
        return _Response([_Block("text", text="完成")], "end_turn")

    monkeypatch.setattr(agent.client.messages, "count_tokens", fake_count)
    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    assert agent.run("精确预检") == "完成"
    assert events == ["count", "create"]
    assert "max_tokens" not in counted[0]
    for key in ("model", "messages", "system", "tools"):
        assert counted[0][key] == created[0][key]
    assert counted[0]["tools"] == [local.schema]


def test_input_safety_margin_uses_user_experience_ten_percent_floor():
    """经验策略：硬窗口的 10% 与 4096 取大者；旧 1% 实现会在 1M 用例稳定失败。"""
    small = Agent(max_input_tokens=20_000, max_tokens=100, check_command=None)
    large = Agent(max_input_tokens=1_000_000, max_tokens=8192, check_command=None)

    assert small._input_safety_margin() == 4096
    assert large._input_safety_margin() == 100_000
    assert large._safe_input_budget() == 1_000_000 - 8192 - 100_000


def test_invalid_explicit_max_input_tokens_is_rejected():
    with pytest.raises(ValueError, match="正整数"):
        Agent(max_input_tokens=0)
    with pytest.raises(ValueError, match="正整数"):
        Agent(max_input_tokens=-1)


def test_count_failure_blocks_inference_and_rolls_back(monkeypatch):
    agent = Agent(
        tools=[],
        max_input_tokens=20_000,
        max_tokens=100,
        check_command=None,
    )
    create_calls = []

    def fail_count(**kwargs):
        raise RuntimeError("COUNT_DOWN_SENTINEL")

    monkeypatch.setattr(agent.client.messages, "count_tokens", fail_count)
    monkeypatch.setattr(
        agent.client.messages,
        "create",
        lambda **kwargs: create_calls.append(kwargs),
    )

    detail = agent.run_detailed("这条入口消息失败后必须回滚")

    assert detail.status == "failed"
    assert detail.failure_code == "preflight_count_failed"
    assert create_calls == []
    assert detail.preflight["request_sent"] is False
    assert detail.usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }, "count_tokens 是免费估算，不得混进真实 Messages usage"
    assert agent.messages == []


def test_hard_limit_after_first_turn_preserves_anchor_and_first_turn(monkeypatch):
    local = LocalTool(
        name="probe",
        description="返回一个很小的探针结果。",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda data: tool_success("PROBE_RESULT_SENTINEL"),
    )
    agent = Agent(
        tools=[],
        local_tools=[local],
        max_iterations=4,
        compact_threshold=999_999,
        max_input_tokens=5_000,
        max_tokens=100,
        check_command=None,
    )
    counts = iter([100, 1_000])  # safe budget = 804；第二轮已知硬超限。
    create_calls = []

    monkeypatch.setattr(
        agent.client.messages,
        "count_tokens",
        lambda **kwargs: _Count(next(counts)),
    )

    def fake_create(**kwargs):
        create_calls.append(copy.deepcopy(kwargs))
        return _Response([
            _Block("tool_use", id="first-tool", name="probe", input={})
        ], "tool_use")

    monkeypatch.setattr(agent.client.messages, "create", fake_create)
    monkeypatch.setattr(agent, "_summarize", lambda prompt: (_ for _ in ()).throw(
        RuntimeError("SUMMARY_INVALID_SENTINEL")
    ))

    detail = agent.run_detailed("LATEST_USER_INPUT_SENTINEL：绝对不能丢")

    assert detail.status == "incomplete"
    assert detail.failure_code == "context_budget_exhausted"
    assert len(create_calls) == 1, "第二轮超预算后不得继续发送推理请求"
    assert detail.preflight["request_sent"] is False
    assert agent.messages[0]["content"] == "LATEST_USER_INPUT_SENTINEL：绝对不能丢"
    first_assistant = agent.messages[1]
    first_result = agent.messages[2]
    assert first_assistant["content"][0].id == "first-tool"
    assert first_result["content"][0]["tool_use_id"] == "first-tool"
    assert "PROBE_RESULT_SENTINEL" in first_result["content"][0]["content"]
    assert _tool_pair_ids(agent.messages)[0] == _tool_pair_ids(agent.messages)[1]
    _assert_adjacent_tool_pairs(agent.messages)


def test_spawned_subagent_inherits_parent_hard_window(monkeypatch):
    parent = Agent(max_input_tokens=30_000, max_tokens=100, check_command=None)
    captured = {}

    class _SubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_detailed(self, task):
            from contextforge.agent import AgentRunResult
            return AgentRunResult(
                status="succeeded", output="ok", usage={
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                }, trace_ref=None, duration_seconds=0, stop_reason="end_turn",
                tool_calls=[], error=None, trace_metadata={},
            )

    monkeypatch.setattr("contextforge.agent.Agent", _SubAgent)
    parent._execute_tool("spawn_subagent", {"task": "继承窗口"})

    assert captured["max_input_tokens"] == 30_000
    assert captured["max_tokens"] == 100


def test_compaction_subagent_inherits_parent_output_reservation(monkeypatch):
    parent = Agent(
        max_input_tokens=10_000,
        max_tokens=100,
        compact_executor="subagent",
        check_command=None,
    )
    captured = {}

    class _SubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_detailed(self, task):
            from contextforge.agent import AgentRunResult
            return AgentRunResult(
                status="succeeded", output="摘要", usage={
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                }, trace_ref=None, duration_seconds=0, stop_reason="end_turn",
                tool_calls=[], error=None, trace_metadata={},
            )

    monkeypatch.setattr("contextforge.agent.Agent", _SubAgent)
    assert parent._summarize_via_subagent("历史") == "摘要"
    assert captured["max_input_tokens"] == 10_000
    assert captured["max_tokens"] == 100
    assert captured["allow_preflight_compaction"] is False


def test_model_context_window_exceeded_pairs_tools_and_sets_failure_code(monkeypatch):
    local = LocalTool(
        name="probe",
        description="上下文窗口停止探针。",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda data: tool_success("不应执行"),
    )
    agent = Agent(
        tools=[],
        local_tools=[local],
        compact_threshold=999_999,
        check_command=None,
    )
    monkeypatch.setattr(agent.client.messages, "count_tokens", lambda **kwargs: _Count(10))
    monkeypatch.setattr(
        agent.client.messages,
        "create",
        lambda **kwargs: _Response([
            _Block("tool_use", id="window-cut", name="probe", input={})
        ], "model_context_window_exceeded"),
    )

    detail = agent.run_detailed("触发服务端真实窗口停止")

    assert detail.status == "incomplete"
    assert detail.failure_code == "context_budget_exhausted"
    assert detail.stop_reason == "model_context_window_exceeded"
    _assert_adjacent_tool_pairs(agent.messages)
    assert detail.tool_calls[0]["executed"] is False


def test_soft_compaction_failure_sends_already_counted_safe_request(monkeypatch):
    agent = Agent(
        tools=[],
        compact_threshold=50,
        max_input_tokens=20_000,
        max_tokens=100,
        check_command=None,
    )
    agent.messages = [{"role": "user", "content": "旧任务"}]
    for index in range(5):
        agent.messages.extend(_turn(index))
    before = copy.deepcopy(agent.messages)
    created = []

    monkeypatch.setattr(agent.client.messages, "count_tokens", lambda **kwargs: _Count(100))
    monkeypatch.setattr(agent, "_summarize", lambda prompt: (_ for _ in ()).throw(
        RuntimeError("SOFT_SUMMARY_FAILURE")
    ))
    monkeypatch.setattr(
        agent.client.messages,
        "create",
        lambda **kwargs: created.append(copy.deepcopy(kwargs)) or _Response([
            _Block("text", text="安全原请求完成")
        ], "end_turn"),
    )

    result = agent.run("最新任务原文")

    assert result == "安全原请求完成"
    assert created, "硬预算安全时应发送已成功 count 的原请求"
    assert created[0]["messages"][:-1] == before
    assert created[0]["messages"][-1]["content"] == "最新任务原文"
    assert agent.messages[-2]["content"] == "最新任务原文"
