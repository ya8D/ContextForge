"""
test_context.py —— P3 上下文压缩的纯逻辑测试（不烧钱、毫秒级）。

覆盖 context.py 里所有「决策/切分」纯逻辑。唯一的副作用（调 LLM 生成摘要）
通过传入**假 summarizer** 回调隔离掉——不调 API、不依赖网络。

跑法：py -m pytest tests/test_context.py -v （属于 "not e2e" 那批）
"""

import context
from context import (
    KEEP_RECENT_TURNS,
    compact_messages,
    current_context_tokens,
    should_compact,
)


# ── current_context_tokens：三字段相加 = 真实发出总量 ──

def test_current_context_tokens_sums_three_fields():
    """规模 = input + cache_read + cache_write（你验证过的 KV Cache 结论）。"""
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 50,
        "output_tokens": 999,  # output 不算「发出去的输入规模」，不应计入
    }
    assert current_context_tokens(usage) == 100 + 900 + 50


def test_current_context_tokens_handles_none():
    """cache 字段可能是 None（getattr 兜底的结果），要当 0 处理，不能报错。"""
    usage = {
        "input_tokens": 200,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }
    assert current_context_tokens(usage) == 200


# ── should_compact：阈值判断 ──

def test_should_compact_below_threshold():
    usage = {"input_tokens": 1000, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    assert should_compact(usage, threshold=500_000) is False


def test_should_compact_at_and_above_threshold():
    # 恰好等于阈值 → 触发（>=）
    usage_eq = {"input_tokens": 500_000, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0}
    assert should_compact(usage_eq, threshold=500_000) is True
    # 超过阈值 → 触发
    usage_gt = {"input_tokens": 400_000, "cache_read_input_tokens": 150_000,
                "cache_creation_input_tokens": 0}
    assert should_compact(usage_gt, threshold=500_000) is True


# ── compact_messages：保头 + 压中段 + 保尾 ──

def _fake_summarizer(prompt: str) -> str:
    """假 summarizer：不调 LLM，直接返回一个固定摘要串。

    这样 compact_messages 的所有决策/切分逻辑都能被测，且完全不烧钱。
    """
    return "这是压缩后的前情摘要。"


def _make_turn(idx: int) -> list[dict]:
    """造一「轮」：一条 assistant(tool_use) + 一条 user(tool_result)，配对完整。"""
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{idx}", "name": "read_file",
             "input": {"path": f"f{idx}.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{idx}",
             "content": f"文件{idx}的内容"}]},
    ]


def _build_history(num_turns: int) -> list[dict]:
    """头（任务）+ num_turns 个完整轮。"""
    msgs: list[dict] = [{"role": "user", "content": "原始任务：分析这些文件"}]
    for i in range(num_turns):
        msgs.extend(_make_turn(i))
    return msgs


def test_compact_keeps_head_and_recent_compresses_middle():
    """5 轮历史、保留最近 3 轮 → 中段 2 轮被压成 1 条摘要。

    验证结构：头 + 摘要 + 最近3轮原文。
    """
    msgs = _build_history(5)  # 1 头 + 5*2 = 11 条
    new_msgs, stats = compact_messages(msgs, _fake_summarizer, keep_recent_turns=3)

    assert stats is not None
    # 头原样保留
    assert new_msgs[0] == msgs[0]
    assert "原始任务" in new_msgs[0]["content"]
    # 第二条是摘要消息，带醒目标记
    assert new_msgs[1]["role"] == "user"
    assert "前情摘要" in new_msgs[1]["content"]
    assert "这是压缩后的前情摘要" in new_msgs[1]["content"]
    # 压缩统计：压掉 2 轮，保留 3 轮
    assert stats["compacted_turns"] == 2
    assert stats["kept_recent_turns"] == 3
    # 结构 = 头(1) + 摘要(1) + 最近3轮(3*2=6) = 8 条
    assert len(new_msgs) == 8
    assert stats["after_msgs"] == 8
    assert stats["before_msgs"] == 11


def test_compact_preserves_tool_use_result_pairing():
    """压缩后，保留段里每个 tool_use 都有配对的 tool_result（不被拆散）。

    这是硬约束：拆散会让 Anthropic API 直接报错。
    """
    msgs = _build_history(5)
    new_msgs, _ = compact_messages(msgs, _fake_summarizer, keep_recent_turns=3)

    # 收集保留段里所有 tool_use id 和 tool_result 的 tool_use_id
    tool_use_ids = set()
    tool_result_ids = set()
    for m in new_msgs:
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_use_ids.add(b["id"])
                elif isinstance(b, dict) and b.get("type") == "tool_result":
                    tool_result_ids.add(b["tool_use_id"])
    # 每个保留下来的 tool_use 都必须有配对 result（反之亦然）
    assert tool_use_ids == tool_result_ids


def test_compact_skips_when_too_few_turns():
    """轮数 <= 保留轮数 → 切不出中段，返回原 messages + None（不假装压了）。"""
    msgs = _build_history(3)  # 恰好等于默认保留轮数
    new_msgs, stats = compact_messages(msgs, _fake_summarizer, keep_recent_turns=3)
    assert stats is None
    assert new_msgs is msgs  # 原样返回，没动


def test_compact_empty_messages():
    """空历史 → 直接返回 (空, None)，不炸。"""
    new_msgs, stats = compact_messages([], _fake_summarizer)
    assert new_msgs == []
    assert stats is None


def test_summarizer_receives_middle_content():
    """验证传给 summarizer 的 prompt 里确实包含中段的工具信息（渲染没丢内容）。"""
    captured = {}

    def capturing_summarizer(prompt: str) -> str:
        captured["prompt"] = prompt
        return "摘要"

    msgs = _build_history(5)
    compact_messages(msgs, capturing_summarizer, keep_recent_turns=3)
    # 中段是第 0、1 轮，prompt 里应能看到 read_file 和被读的文件名
    assert "read_file" in captured["prompt"]
    assert "f0.txt" in captured["prompt"] or "文件0" in captured["prompt"]
