"""
test_agent_logic.py —— agent.py 里纯逻辑函数的测试（不调 API，不构造 Agent）。

只测模块级纯函数：_truncate_for_feedback（回喂截断）、_to_serializable、
_log（T1 日志分级）、_dump_turn（T1 trace 开关）。
不测完整 TAOR 循环 —— 那个要真调 API，见 test_e2e.py。

跑法：py -m pytest tests/test_agent_logic.py -v
"""

from contextforge.agent import Agent, _MAX_RESULT_CHARS, _log, _to_serializable, _truncate_for_feedback


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


# ── _log（T1：CONTEXTFORGE_LOG 分级，off/normal/debug + level="error" 兜底）────

def test_log_error_level_prints_even_when_off(monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTFORGE_LOG", "off")
    _log("🛡️ [权限]", "拦截了危险命令", level="error")
    assert "拦截了危险命令" in capsys.readouterr().out


def test_log_normal_level_suppressed_when_off(monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTFORGE_LOG", "off")
    _log("🧠 [Think]", "普通进度行")
    assert capsys.readouterr().out == ""


def test_log_normal_level_prints_on_default_and_normal(monkeypatch, capsys):
    monkeypatch.delenv("CONTEXTFORGE_LOG", raising=False)
    _log("🧠 [Think]", "默认档可见")
    assert "默认档可见" in capsys.readouterr().out

    monkeypatch.setenv("CONTEXTFORGE_LOG", "normal")
    _log("🧠 [Think]", "normal档可见")
    assert "normal档可见" in capsys.readouterr().out


def test_log_debug_level_suppressed_on_normal(monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTFORGE_LOG", "normal")
    _log("📊 [debug]", "细节行", level="debug")
    assert capsys.readouterr().out == ""


def test_log_debug_level_prints_on_debug(monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTFORGE_LOG", "debug")
    _log("📊 [debug]", "细节行", level="debug")
    assert "细节行" in capsys.readouterr().out


def test_log_invalid_value_falls_back_to_normal(monkeypatch, capsys):
    # 非法值兜底当 normal 处理：不报错、普通行仍打印、debug 行仍吞掉
    monkeypatch.setenv("CONTEXTFORGE_LOG", "xyz")
    _log("🧠 [Think]", "普通行")
    assert "普通行" in capsys.readouterr().out
    _log("📊 [debug]", "细节行", level="debug")
    assert capsys.readouterr().out == ""


# ── _dump_turn（T1：CONTEXTFORGE_TRACE 独立控落盘，屏幕日志不受影响）────────

def test_dump_turn_writes_file_when_trace_on(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTEXTFORGE_TRACE", raising=False)  # 默认 on
    agent = Agent()
    agent.current_task_dir = tmp_path
    agent._dump_turn(1, [], {"input_tokens": 1}, "end_turn")
    assert (tmp_path / "turn_01.json").exists()


def test_dump_turn_skips_when_trace_off(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    agent = Agent()
    agent.current_task_dir = tmp_path
    agent._dump_turn(1, [], {"input_tokens": 1}, "end_turn")
    assert not (tmp_path / "turn_01.json").exists()


def test_dump_turn_records_response_content(monkeypatch, tmp_path):
    """trace 补记模型输出：本轮回复内容真的进了 turn_NN.json 的 response_content 字段。

    用假 content block（仿 SDK 的 TextBlock，有 model_dump()）模拟 response.content，
    不调 API。验证的正是用户发现缺失的东西：一轮 end_turn 结束时，回复文字也能落盘复盘。
    """
    import json

    class FakeBlock:
        def model_dump(self):
            return {"type": "text", "text": "模型回复XYZ"}

    monkeypatch.delenv("CONTEXTFORGE_TRACE", raising=False)  # 默认 on
    agent = Agent()
    agent.current_task_dir = tmp_path
    agent._dump_turn(1, [{"role": "user", "content": "问题"}],
                     {"input_tokens": 1}, "end_turn", [FakeBlock()])

    data = json.loads((tmp_path / "turn_01.json").read_text(encoding="utf-8"))
    assert "response_content" in data
    assert data["response_content"] == [{"type": "text", "text": "模型回复XYZ"}]


# ── T5-A：客制化 compact（会话级偏好 + 执行者切换 + compact_now）────────

def test_agent_stores_compact_directive_and_executor_defaults(monkeypatch):
    """会话级偏好存住；执行者默认 self、可设 subagent。

    显式 delenv 保证不受 .env / 环境里的 compact 相关变量干扰（它们是新增的环境兜底来源）。
    """
    monkeypatch.delenv("CONTEXTFORGE_COMPACT_DIRECTIVE", raising=False)
    monkeypatch.delenv("CONTEXTFORGE_COMPACT_EXECUTOR", raising=False)
    monkeypatch.delenv("CONTEXTFORGE_COMPACT_THRESHOLD", raising=False)
    a = Agent()
    assert a.compact_directive is None
    assert a.compact_executor == "self"
    b = Agent(compact_directive="保留登录报错", compact_executor="subagent")
    assert b.compact_directive == "保留登录报错"
    assert b.compact_executor == "subagent"


def test_compact_directive_reads_env_fallback(monkeypatch):
    """会话级偏好可从环境变量 CONTEXTFORGE_COMPACT_DIRECTIVE 兜底（写 .env 持久生效）。"""
    monkeypatch.setenv("CONTEXTFORGE_COMPACT_DIRECTIVE", "环境里设的压缩偏好")
    a = Agent()  # 不显式传参
    assert a.compact_directive == "环境里设的压缩偏好"


def test_explicit_compact_directive_overrides_env(monkeypatch):
    """显式传参优先级高于环境变量（同 self.model 读 ANTHROPIC_MODEL 的兜底语义）。"""
    monkeypatch.setenv("CONTEXTFORGE_COMPACT_DIRECTIVE", "环境偏好")
    a = Agent(compact_directive="显式偏好")
    assert a.compact_directive == "显式偏好"


# ── 验证门检查命令：显式 > CONTEXTFORGE_CHECK_COMMAND > None（跳过）──

def test_check_command_reads_env_fallback(monkeypatch):
    """验证门命令可从环境变量 CONTEXTFORGE_CHECK_COMMAND 兜底，且同步进门。"""
    monkeypatch.setenv("CONTEXTFORGE_CHECK_COMMAND", "py -m pytest -q")
    a = Agent()  # 不显式传参
    assert a.check_command == "py -m pytest -q"
    assert a.validation_gate.check_command == "py -m pytest -q"


def test_explicit_check_command_overrides_env(monkeypatch):
    """显式传参优先级高于环境变量（同 compact_directive 的兜底语义）。"""
    monkeypatch.setenv("CONTEXTFORGE_CHECK_COMMAND", "环境命令")
    a = Agent(check_command="py -m pytest tests/test_x.py")
    assert a.check_command == "py -m pytest tests/test_x.py"
    assert a.validation_gate.check_command == "py -m pytest tests/test_x.py"


def test_check_command_default_none_skips_gate(monkeypatch):
    """不传不设环境 → check_command 为 None，验证门无条件放行（不添乱）。"""
    monkeypatch.delenv("CONTEXTFORGE_CHECK_COMMAND", raising=False)
    a = Agent()
    assert a.check_command is None
    passed, _ = a.validation_gate.verify(lambda cmd: "")  # runner 不会被调到
    assert passed is True


# ── 压缩触发阈值：显式 > CONTEXTFORGE_COMPACT_THRESHOLD > 默认（支撑 Chromium 大上下文）──

def test_compact_threshold_default(monkeypatch):
    """不传不设环境 → 用默认 500K。"""
    from contextforge.context import COMPACT_THRESHOLD_TOKENS
    monkeypatch.delenv("CONTEXTFORGE_COMPACT_THRESHOLD", raising=False)
    assert Agent().compact_threshold == COMPACT_THRESHOLD_TOKENS


def test_compact_threshold_reads_env(monkeypatch):
    """CONTEXTFORGE_COMPACT_THRESHOLD 环境兜底：调高阈值让大项目用满更多上下文再压。"""
    monkeypatch.setenv("CONTEXTFORGE_COMPACT_THRESHOLD", "800000")
    assert Agent().compact_threshold == 800_000


def test_explicit_compact_threshold_overrides_env(monkeypatch):
    """显式传参优先于环境变量。"""
    monkeypatch.setenv("CONTEXTFORGE_COMPACT_THRESHOLD", "800000")
    assert Agent(compact_threshold=1234).compact_threshold == 1234


def test_compact_threshold_invalid_env_falls_back(monkeypatch):
    """环境变量非法（非正整数）→ 兜底回默认，不报错。"""
    from contextforge.context import COMPACT_THRESHOLD_TOKENS
    for bad in ["abc", "-5", "0", ""]:
        monkeypatch.setenv("CONTEXTFORGE_COMPACT_THRESHOLD", bad)
        assert Agent().compact_threshold == COMPACT_THRESHOLD_TOKENS


def test_compact_threshold_1m_80pct_trigger_boundary(monkeypatch):
    """把阈值设成 1M 窗口的 80%（800K）→ 上下文到 800K 才触发压缩，799,999 不触发。

    这验证的不只是"阈值被解析对了"，而是它真的被用作压缩判据：Chromium 这类大项目
    想用满更多上下文再压，就靠调高这个阈值。用真实 usage 三字段（input+cache_read+
    cache_write，= 发出去的真实规模）构造刚好卡在边界两侧的规模。
    """
    from contextforge.context import should_compact

    monkeypatch.setenv("CONTEXTFORGE_COMPACT_THRESHOLD", "800000")  # 1,000,000 * 0.8
    threshold = Agent().compact_threshold
    assert threshold == 800_000

    # 刚好差 1 token 到阈值 → 不压（799,999 < 800,000）
    just_below = {"input_tokens": 799_999, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}
    assert should_compact(just_below, threshold=threshold) is False

    # 恰好等于阈值 → 触发（>=）
    at_threshold = {"input_tokens": 500_000, "cache_read_input_tokens": 300_000,
                    "cache_creation_input_tokens": 0}
    assert should_compact(at_threshold, threshold=threshold) is True

    # 超过阈值 → 触发
    above = {"input_tokens": 400_000, "cache_read_input_tokens": 400_000,
             "cache_creation_input_tokens": 1}
    assert should_compact(above, threshold=threshold) is True


# ── 压缩执行者：显式 > CONTEXTFORGE_COMPACT_EXECUTOR > "self"（让子 agent 核实成为可选操作）──

def test_compact_executor_reads_env(monkeypatch):
    """CONTEXTFORGE_COMPACT_EXECUTOR 环境兜底：从命令行也能切到子 agent 回读核实。"""
    monkeypatch.setenv("CONTEXTFORGE_COMPACT_EXECUTOR", "subagent")
    assert Agent().compact_executor == "subagent"


def test_explicit_compact_executor_overrides_env(monkeypatch):
    """显式传参优先于环境变量。"""
    monkeypatch.setenv("CONTEXTFORGE_COMPACT_EXECUTOR", "subagent")
    assert Agent(compact_executor="self").compact_executor == "self"


def test_pick_summarizer_switches_by_executor():
    """_pick_summarizer 按 executor 选回调：self→_summarize，subagent→_summarize_via_subagent。"""
    a = Agent()
    assert a._pick_summarizer() == a._summarize
    b = Agent(compact_executor="subagent")
    assert b._pick_summarizer() == b._summarize_via_subagent


def test_compact_now_too_few_turns_returns_not_compacted():
    """主动压缩：历史短到连降级路径都压不了（只有头）→ 返回"未压缩"，messages 不变。"""
    a = Agent()
    a.messages = [{"role": "user", "content": "任务"}]  # 只有头，切不出任何轮
    before = list(a.messages)
    result = a.compact_now(directive="随便")
    assert "未压缩" in result
    assert a.messages == before


def _seed_ab(a):
    """给 agent 造 [A(user任务), B(assistant回答)] 两条——真实直答型短会话。

    /compact 指令不进历史，故用户主动压时历史往往就是这两条，想压的正是 B。
    """
    a.messages = [
        {"role": "user", "content": "原始任务A：列出百家姓前一百"},
        {"role": "assistant", "content": "B：赵钱孙李…（前一百排名全文）"},
    ]


def test_compact_now_falls_back_to_directive_when_too_few_turns(monkeypatch):
    """轮数不足以走结构化压缩、但带了 directive → 降级为指令驱动压缩，真压。"""
    captured = {}

    def fake_summarize(prompt: str) -> str:
        captured["prompt"] = prompt
        return "假摘要"

    a = Agent()
    monkeypatch.setattr(a, "_summarize", fake_summarize)
    _seed_ab(a)

    result = a.compact_now(directive="只保留结论，删掉过程")
    assert "已压缩" in result
    assert len(a.messages) == 2                       # [头, 摘要]
    assert a.messages[0]["content"] == "原始任务A：列出百家姓前一百"  # 保首条
    assert "前情摘要" in a.messages[1]["content"]      # 其余压成摘要
    assert "B：赵钱孙李" in captured["prompt"]         # 要压的 B 进了 prompt
    assert "只保留结论，删掉过程" in captured["prompt"]
    # 走的是「纯指令」路径，prompt 不应带四维基础要求
    assert "任务目标是什么" not in captured["prompt"]


def test_compact_now_bare_compact_too_few_turns_not_compacted(monkeypatch):
    """轮数不足 + 裸 /compact（无 directive、无会话级偏好）→ 不触发降级，仍不压。"""
    a = Agent()  # 无 compact_directive
    monkeypatch.setattr(a, "_summarize", lambda p: "不该被调到")
    _seed_ab(a)
    before = list(a.messages)
    result = a.compact_now()  # 不传 directive
    assert "未压缩" in result
    assert a.messages == before


def test_compact_now_compacts_and_passes_directive(monkeypatch):
    """主动压缩：够轮数 → 用（假）summarizer 压缩，directive 透传进 prompt，messages 变短。"""
    captured = {}

    def fake_summarize(prompt: str) -> str:
        captured["prompt"] = prompt
        return "假摘要"

    a = Agent()
    # 把执行者回调替换成假的，不烧钱、不调 API。
    monkeypatch.setattr(a, "_summarize", fake_summarize)
    # 造头 + 5 个完整轮（够压：默认保留最近 3 轮，中段 2 轮可压）。
    a.messages = [{"role": "user", "content": "原始任务"}]
    for i in range(5):
        a.messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "read_file",
             "input": {"path": f"f{i}.txt"}}]})
        a.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": f"内容{i}"}]})
    before_len = len(a.messages)

    result = a.compact_now(directive="只保留 f0 相关")
    assert "已压缩" in result
    assert len(a.messages) < before_len            # 真的变短了
    assert "只保留 f0 相关" in captured["prompt"]    # directive 进了 prompt


def test_compact_now_falls_back_to_session_directive(monkeypatch):
    """主动压缩不传 directive 时，回退到会话级 self.compact_directive。"""
    captured = {}

    def fake_summarize(prompt: str) -> str:
        captured["prompt"] = prompt
        return "假摘要"

    a = Agent(compact_directive="会话级要求ABC")
    monkeypatch.setattr(a, "_summarize", fake_summarize)
    a.messages = [{"role": "user", "content": "原始任务"}]
    for i in range(5):
        a.messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "read_file",
             "input": {"path": f"f{i}.txt"}}]})
        a.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": f"内容{i}"}]})

    a.compact_now()  # 不传 directive
    assert "会话级要求ABC" in captured["prompt"]
