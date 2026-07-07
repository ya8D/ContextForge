"""
test_agent_logic.py —— agent.py 里纯逻辑函数的测试（不调 API，不构造 Agent）。

只测模块级纯函数：_truncate_for_feedback（回喂截断）、_to_serializable。
不测完整 TAOR 循环 —— 那个要真调 API，见 test_e2e.py。

跑法：py -m pytest tests/test_agent_logic.py -v
"""

from agent import _MAX_RESULT_CHARS, _to_serializable, _truncate_for_feedback


# ── _truncate_for_feedback（回喂截断，防上下文爆炸）──────────────

def test_short_result_passes_through():
    # 短于上限的结果原样返回，不加任何东西
    short = "这是一段很短的输出"
    assert _truncate_for_feedback(short) == short


def test_long_result_is_truncated():
    long = "x" * (_MAX_RESULT_CHARS + 5000)  # 明显超过上限
    out = _truncate_for_feedback(long)
    # 截断后应远短于原文
    assert len(out) < len(long)
    # 保留了头部（前 _MAX_RESULT_CHARS 个字符）
    assert out.startswith("x" * _MAX_RESULT_CHARS)
    # 带了截断提示，且提示里包含原文长度，给模型自我纠正的线索
    assert "截断" in out
    assert str(len(long)) in out
    assert "分段读取" in out


def test_result_exactly_at_limit_not_truncated():
    # 正好等于上限：不截断（边界条件）
    exact = "y" * _MAX_RESULT_CHARS
    assert _truncate_for_feedback(exact) == exact


# ── _to_serializable（把混合内容转成可 JSON 序列化）─────────────

def test_serializable_plain_dict_and_list():
    data = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    # 纯 dict/list 应原样返回（可被 json.dumps）
    assert _to_serializable(data) == data


def test_serializable_handles_pydantic_like_object():
    # 模拟 anthropic SDK 的 content block（有 model_dump 方法）
    class FakeBlock:
        def model_dump(self):
            return {"type": "text", "text": "from_sdk"}

    out = _to_serializable([FakeBlock()])
    assert out == [{"type": "text", "text": "from_sdk"}]
