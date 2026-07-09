"""
test_context.py —— P3 上下文压缩的纯逻辑测试（不烧钱、毫秒级）。

覆盖 context.py 里所有「决策/切分」纯逻辑。唯一的副作用（调 LLM 生成摘要）
通过传入**假 summarizer** 回调隔离掉——不调 API、不依赖网络。

跑法：py -m pytest tests/test_context.py -v （属于 "not e2e" 那批）
"""

from contextforge import context
from contextforge.context import (
    KEEP_RECENT_TURNS,
    compact_by_directive,
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


# ── T5-A：directive 客制化压缩偏好（向后兼容 + 叠加注入）──

def test_directive_none_prompt_identical_to_p3():
    """directive=None 时，传给 summarizer 的 prompt 与 P3 逐字相同（向后兼容钉死）。"""
    from contextforge.context import _SUMMARY_PROMPT

    captured = {}

    def capturing(prompt: str) -> str:
        captured["prompt"] = prompt
        return "摘要"

    msgs = _build_history(5)
    compact_messages(msgs, capturing, keep_recent_turns=3, directive=None)
    # prompt = _SUMMARY_PROMPT + 中段渲染文本；开头必须逐字是 _SUMMARY_PROMPT
    assert captured["prompt"].startswith(_SUMMARY_PROMPT)


def test_directive_injected_into_prompt_and_keeps_base():
    """directive 有值时：该串出现在 prompt 里，且四维基础要求仍在（叠加不替换）。"""
    captured = {}

    def capturing(prompt: str) -> str:
        captured["prompt"] = prompt
        return "摘要"

    msgs = _build_history(5)
    compact_messages(msgs, capturing, keep_recent_turns=3,
                     directive="重点保留登录相关报错")
    # 用户要求进了 prompt
    assert "重点保留登录相关报错" in captured["prompt"]
    # 四维基础要求（底线）仍在，没被替换掉
    assert "任务目标是什么" in captured["prompt"]
    assert "下一步要往哪走" in captured["prompt"]


def test_directive_recorded_in_summary_marker():
    """directive 会写进摘要消息的标记，便于回看 trace 时知道这次压缩的目的。"""
    msgs = _build_history(5)
    new_msgs, _ = compact_messages(msgs, _fake_summarizer, keep_recent_turns=3,
                                   directive="只保留数据库 schema")
    summary_msg = new_msgs[1]  # 头之后第一条就是摘要
    assert "前情摘要" in summary_msg["content"]
    assert "只保留数据库 schema" in summary_msg["content"]


# ── 指令驱动压缩（compact_by_directive）：保首条 + 按 directive 压其余 ──

def _ab_messages():
    """复刻真实场景：A(user问题) + B(assistant回答)。

    /compact 指令不进历史，故用户主动压时历史往往就是这两条，想压的正是 B。
    """
    return [
        {"role": "user", "content": "A：百家姓前一百排名是怎样的？存到临时上下文。"},
        {"role": "assistant", "content": "B：1-50 赵钱孙李…；51-100 昌马苗凤…（全文排名）"},
    ]


def test_compact_by_directive_keeps_head_compacts_rest():
    """带 directive → 压成 [头, 摘要]：保首条、其余换成含 marker 的摘要。"""
    msgs = _ab_messages()
    new_msgs, stats = compact_by_directive(msgs, _fake_summarizer,
                                           directive="只保留结论，删掉过程")
    assert stats is not None
    assert len(new_msgs) == 2
    assert new_msgs[0] is msgs[0]                  # 保首条（同一对象）
    assert "前情摘要" in new_msgs[1]["content"]     # 其余压成摘要
    assert "只保留结论，删掉过程" in new_msgs[1]["content"]
    assert stats["kept_recent_turns"] == 0         # 不单独保末条


def test_compact_by_directive_two_message_history():
    """用户实测场景：直答型 [A, B] 两条历史也必须能压（上一版核心缺陷）。"""
    captured = {}

    def capturing(prompt: str) -> str:
        captured["prompt"] = prompt
        return "压缩后的摘要"

    msgs = _ab_messages()
    new_msgs, stats = compact_by_directive(msgs, capturing, directive="删掉后五十名的排名")
    assert stats is not None
    assert len(new_msgs) == 2
    assert "B：1-50" in captured["prompt"]          # 要压的 B 进了 prompt
    assert "删掉后五十名的排名" in captured["prompt"] # directive 也进了
    assert new_msgs[0]["content"].startswith("A：") # A 保住


def test_compact_by_directive_prompt_is_directive_only():
    """降级路径的 prompt 含 directive，但**不含**四维基础要求（纯按指令压）。"""
    captured = {}

    def capturing(prompt: str) -> str:
        captured["prompt"] = prompt
        return "摘要"

    compact_by_directive(_ab_messages(), capturing, directive="删掉后50名")
    assert "删掉后50名" in captured["prompt"]
    # 四维基础要求（_SUMMARY_PROMPT 的特征串）不应出现——这条是「完全听用户的」
    assert "任务目标是什么" not in captured["prompt"]
    assert "下一步要往哪走" not in captured["prompt"]


def test_compact_by_directive_needs_directive():
    """没有 directive（None / 空串）→ 不压，返回 (原样, None)。"""
    msgs = _ab_messages()
    assert compact_by_directive(msgs, _fake_summarizer, directive=None) == (msgs, None)
    assert compact_by_directive(msgs, _fake_summarizer, directive="") == (msgs, None)


def test_compact_by_directive_too_short():
    """消息不足 2 条（没有可压的其余）→ 不压。"""
    one = [{"role": "user", "content": "A"}]
    assert compact_by_directive(one, _fake_summarizer, directive="随便压")[1] is None
    assert compact_by_directive([], _fake_summarizer, directive="随便压")[1] is None
