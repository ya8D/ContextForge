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
    # runner 用当前双参契约 (cmd, timeout)->(code, out)；check_command=None 时 verify 提前 return、
    # 根本不调它。用哨兵让「万一被调到」立刻暴露，而非用签名不符的单参 lambda 掩盖回归。
    def _should_not_run(cmd, timeout):
        raise AssertionError("check_command=None 时不该调 runner")
    passed, _ = a.validation_gate.verify(_should_not_run)
    assert passed is True


def test_explicit_none_check_command_opts_out_over_env(monkeypatch):
    """显式 check_command=None **压过**环境变量 → 关掉验证门（子 agent 就靠这个不继承）。

    回归防护：`__init__` 曾用 `check_command or os.environ.get(...)`，`None or env` 会落到 env，
    导致「显式传 None 想关掉」被环境变量悄悄覆盖——子 agent 因此继承主任务的 check_command、
    被无关检查命令反复打回撞 max_iterations。改用哨兵区分「没传」与「显式 None」后此处必须为 None。
    """
    monkeypatch.setenv("CONTEXTFORGE_CHECK_COMMAND", "env_should_be_ignored")
    a = Agent(check_command=None)          # 显式关掉
    assert a.check_command is None, "显式 None 被环境变量覆盖了（哨兵区分失效）"
    assert a.validation_gate.check_command is None
    # 对照：不传才读环境
    b = Agent()
    assert b.check_command == "env_should_be_ignored"


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


# ── P2：单轮输出上限 max_tokens：显式 > CONTEXTFORGE_MAX_TOKENS > 默认 8192，且真被两处调用点用上 ──

def test_max_tokens_default(monkeypatch):
    """不传不设环境 → 用默认 8192（原硬编码 2048 过小）。"""
    from contextforge.agent import MAX_TOKENS_DEFAULT
    monkeypatch.delenv("CONTEXTFORGE_MAX_TOKENS", raising=False)
    assert Agent().max_tokens == MAX_TOKENS_DEFAULT == 8192


def test_max_tokens_reads_env(monkeypatch):
    """CONTEXTFORGE_MAX_TOKENS 环境兜底：写大文件需要更长单轮输出时可调高。"""
    monkeypatch.setenv("CONTEXTFORGE_MAX_TOKENS", "16000")
    assert Agent().max_tokens == 16000


def test_explicit_max_tokens_overrides_env(monkeypatch):
    """显式传参优先于环境变量。"""
    monkeypatch.setenv("CONTEXTFORGE_MAX_TOKENS", "16000")
    assert Agent(max_tokens=4000).max_tokens == 4000


def test_max_tokens_invalid_env_falls_back(monkeypatch):
    """环境变量非法（非正整数）→ 兜底回默认 8192，不报错。"""
    from contextforge.agent import MAX_TOKENS_DEFAULT
    for bad in ["abc", "-5", "0", ""]:
        monkeypatch.setenv("CONTEXTFORGE_MAX_TOKENS", bad)
        assert Agent().max_tokens == MAX_TOKENS_DEFAULT


def test_max_tokens_actually_used_in_api_call(monkeypatch):
    """关键（非恒真）：Think 主调用真的把 self.max_tokens 传给 messages.create——不是还写死 2048。

    在基线（硬编码 max_tokens=2048）上，无论 self.max_tokens 设成什么，create 收到的都是 2048
    → 本断言 fail。修复后 create 收到 self.max_tokens（这里设成 5000）→ pass。这测的是「配置真正
    生效」，不是「配置被解析对了」——后者可能解析对了但调用点没用上。
    """
    import unittest.mock as mock
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    monkeypatch.setenv("CONTEXTFORGE_LOG", "off")
    monkeypatch.delenv("CONTEXTFORGE_MAX_TOKENS", raising=False)
    a = Agent(max_iterations=2, max_tokens=5000)
    captured = {}

    def fake_create(**kw):
        captured["max_tokens"] = kw.get("max_tokens")
        return _FakeResp([_FakeTUBlock("text", text="done")], "end_turn")

    with mock.patch.object(a.client.messages, "create", side_effect=fake_create):
        a.run("随便问")
    assert captured["max_tokens"] == 5000, (
        f"Think 调用传给 API 的 max_tokens={captured.get('max_tokens')}，不是实例的 5000 —— "
        f"说明调用点还写死了值（如 2048），配置没真正生效"
    )


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


# ── 架构修正：实例状态隔离 / trace 防撞名 / 循环检测每任务清零 ──

def test_each_agent_has_independent_read_files():
    """read_files 是**实例状态**：两个 Agent（含子 agent 场景）各一套，互不共享。"""
    a, b = Agent(), Agent()
    assert a.read_files == set() and b.read_files == set()
    a.read_files.add("/some/path.txt")
    assert "/some/path.txt" not in b.read_files   # b 不受 a 影响 → 隔离
    assert a.read_files is not b.read_files        # 不是同一个对象（更不是模块全局）


def test_trace_dir_unique_per_instance():
    """同秒并行创建的多个 Agent，trace 目录靠末尾短 id 区分，不撞名（否则子 agent 覆盖彼此 trace）。"""
    dirs = {str(Agent().trace_dir) for _ in range(5)}
    assert len(dirs) == 5                          # 5 个实例 5 个不同目录


def test_loop_detector_per_instance_and_resettable():
    """死循环检测器是实例状态、可清零（run() 每任务开头会 reset，避免跨任务指纹串味）。"""
    a, b = Agent(), Agent()
    assert a.loop_detector is not b.loop_detector  # 各自独立
    a.loop_detector._recent.extend(["x", "x", "x"])
    a.loop_detector.reset()                        # run() 开头调的就是它
    assert a.loop_detector._recent == []           # 清零后不残留上个任务的指纹


def test_run_rolls_back_messages_on_failure():
    """run() 中途抛异常 → 回滚本任务追加的消息，不留悬挂 user，避免污染下个任务。"""
    import unittest.mock as mock
    a = Agent()
    a.messages = [{"role": "user", "content": "旧任务"},
                  {"role": "assistant", "content": "旧回复"}]  # 已有干净历史
    before = list(a.messages)
    # 第一轮 API 就抛 → 本任务应整体回滚
    with mock.patch.object(a.client.messages, "create", side_effect=RuntimeError("模拟API抖动")):
        try:
            a.run("会失败的任务")
        except RuntimeError:
            pass
    assert a.messages == before                    # 悬挂的 user「会失败的任务」已被回滚，历史干净
    # 连续两条 user 不该出现
    assert not any(a.messages[i]["role"] == "user" and a.messages[i + 1]["role"] == "user"
                   for i in range(len(a.messages) - 1))


def test_run_rollback_survives_midtask_compaction():
    """关键回归：本任务中途触发压缩（messages 被整体重写变短）后再失败 → 快照回滚仍精确还原。

    索引式回滚（del [旧长度:]）在这里会失效：压缩换了 list、旧长度失真，截不掉悬挂消息。
    快照式回滚（self.messages[:] = 快照）无视中途重写，能还原到任务开始前。
    """
    import unittest.mock as mock
    a = Agent()
    # 干净的旧历史（够长，能被压缩真的压）
    a.messages = [{"role": "user", "content": "旧任务"}]
    for i in range(5):
        a.messages.append({"role": "assistant", "content": f"步骤{i}"})
        a.messages.append({"role": "user", "content": f"继续{i}"})
    before = list(a.messages)

    # 模拟 _run_loop：先把 self.messages 整体换成一条更短的新 list（模拟压缩改写），再抛异常。
    def fake_loop():
        a.messages = [a.messages[0], {"role": "user", "content": "[前情摘要] …"}]  # 换新 list、变短
        raise RuntimeError("压缩之后才抖动")

    with mock.patch.object(a, "_run_loop", side_effect=fake_loop):
        try:
            a.run("会失败的任务")
        except RuntimeError:
            pass
    assert a.messages == before                    # 尽管中途压缩改写过，仍精确还原到干净历史
    assert not any(a.messages[i]["role"] == "user" and a.messages[i + 1]["role"] == "user"
                   for i in range(len(a.messages) - 1))


# ── 审查 #1/#2：harness 不执行工具时必须补配对 tool_result（否则下一轮 API 400）──

class _FakeTUBlock:
    """仿 SDK 的 content block；type 可为 text / tool_use。"""
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeResp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


def _unpaired_tool_uses(messages):
    """返回 messages 里所有「没有紧邻配对 tool_result」的 tool_use id（非空=会被 API 400）。"""
    unpaired = []
    for i, m in enumerate(messages):
        if m["role"] != "assistant" or not isinstance(m["content"], list):
            continue
        tu_ids = [b.id for b in m["content"] if getattr(b, "type", None) == "tool_use"]
        result_ids = set()
        if i + 1 < len(messages) and messages[i + 1]["role"] == "user" \
                and isinstance(messages[i + 1]["content"], list):
            result_ids = {b.get("tool_use_id") for b in messages[i + 1]["content"]
                          if isinstance(b, dict) and b.get("type") == "tool_result"}
        unpaired += [t for t in tu_ids if t not in result_ids]
    return unpaired


def test_loop_break_pairs_pending_tool_uses(monkeypatch):
    """审查 #1：死循环打断后，本轮未执行的 tool_use 必须有配对 tool_result，历史不留未配对块。"""
    import unittest.mock as mock
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    monkeypatch.setenv("CONTEXTFORGE_LOG", "off")
    a = Agent(max_iterations=6)
    n = {"i": 0}

    def fake_create(**kw):
        n["i"] += 1
        # 每轮返回同一个工具调用（同命令）→ 连续 3 轮触发 is_looping
        return _FakeResp([_FakeTUBlock("tool_use", id=f"c{n['i']}", name="run_command",
                                       input={"command": "echo stuck"})], "tool_use")

    with mock.patch.object(a.client.messages, "create", side_effect=fake_create):
        a.run("反复卡同一个命令")     # 不应抛异常（补了配对就不会 400）
    assert _unpaired_tool_uses(a.messages) == []


def test_loop_rejects_dangerous_command_and_feeds_back(monkeypatch):
    """P4 循环内权限拦截（纯逻辑版，钉死连线，不受模型意愿影响）。

    背景：e2e 版 test_harness_blocks_dangerous_command 依赖「真实模型愿意去调危险命令」，
    但 Opus 会自查/拒绝作死，导致 harness 没机会触发、断言偶发红（本轮审查发现）。
    「拦截 + 回喂 is_error」这条**循环内**连线本身是确定性的，用假 client 直接钉死：
    第 1 轮假模型请求一个危险命令（git reset --hard）→ 循环应拦下、回喂「被 harness 拒绝」、
    **不真执行**；第 2 轮假模型 end_turn 收尾。断言：历史里有拒绝回喂、且循环没崩正常返回。
    """
    import unittest.mock as mock
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    monkeypatch.setenv("CONTEXTFORGE_LOG", "off")
    a = Agent(max_iterations=4)
    n = {"i": 0}

    def fake_create(**kw):
        n["i"] += 1
        if n["i"] == 1:
            # 第 1 轮：请求一个 harness 必拦的危险命令
            return _FakeResp([_FakeTUBlock("tool_use", id="d1", name="run_command",
                                           input={"command": "git reset --hard HEAD"})], "tool_use")
        # 第 2 轮：拿到拒绝回喂后收尾
        return _FakeResp([_FakeTUBlock("text", text="好的，该命令被拒绝，我不执行了。")], "end_turn")

    with mock.patch.object(a.client.messages, "create", side_effect=fake_create):
        final = a.run("帮我 git reset --hard")

    # ① 循环没崩，正常返回
    assert isinstance(final, str) and final.strip()
    # ② 历史里有「被 harness 拒绝」回喂（拦截真的在循环里触发并回喂）
    saw_rejection = any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        and "被 harness 拒绝" in str(b.get("content", ""))
        for m in a.messages if isinstance(m.get("content"), list)
        for b in m["content"]
    )
    assert saw_rejection, "循环没把危险命令的拒绝回喂进历史 —— 权限拦截连线断了"
    # ③ 危险命令的 tool_use 仍有配对 tool_result（拒绝也要配对，否则下轮 400）
    assert _unpaired_tool_uses(a.messages) == []



# ── P1：一轮里工具按并发安全分批——只读并发、有副作用串行（消除同轮写竞态）──
#
# ★ 有效性设计（吸取评测教训）：不用「同轮写 N 次同一文件、断言最终恒为最后一个」这种判据
# ——那依赖线程调度巧合，在未修复的基线上也常 pass（是恒真假测试）。改用**确定性的并发探针**：
# monkeypatch execute_tool 记录「同一时刻有几个 write_file 在执行」，断言峰值并发度=1（串行）。
# 未修复基线把 write_file 也丢进 8 worker 池，峰值并发度必 >1 → 该测试在基线稳定 fail。

def _drive_one_round(agent, blocks, monkeypatch, probe):
    """驱动 agent 的真实 _run_loop 跑一轮：第 1 轮请求 blocks 里的工具，第 2 轮 end_turn 收尾。
    probe 是一个 (name)->None 的记录回调，包在 execute_tool 外层，用来观测并发。"""
    import unittest.mock as mock
    import contextforge.agent as agent_mod
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    monkeypatch.setenv("CONTEXTFORGE_LOG", "off")
    n = {"i": 0}

    def fake_create(**kw):
        n["i"] += 1
        if n["i"] == 1:
            return _FakeResp(blocks, "tool_use")
        return _FakeResp([_FakeTUBlock("text", text="完成。")], "end_turn")

    real_execute = agent_mod.execute_tool

    def probing_execute(name, tool_input, read_files=None):
        return probe(name, lambda: real_execute(name, tool_input, read_files=read_files))

    monkeypatch.setattr(agent_mod, "execute_tool", probing_execute)
    with mock.patch.object(agent.client.messages, "create", side_effect=fake_create):
        agent.run("一轮多工具")


class _ConcurrencyProbe:
    """记录每个工具「同时在执行的实例数」峰值。用锁+短 sleep 放大并发窗口，让并行真的重叠。"""
    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self.active = {}      # name -> 当前在跑的实例数
        self.peak = {}        # name -> 峰值

    def __call__(self, name, run):
        import time
        with self._lock:
            self.active[name] = self.active.get(name, 0) + 1
            self.peak[name] = max(self.peak.get(name, 0), self.active[name])
        try:
            time.sleep(0.02)   # 放大并发窗口：若真并行，多个实例会在这重叠
            return run()
        finally:
            with self._lock:
                self.active[name] -= 1


def test_side_effecting_tools_run_serially_in_one_round(monkeypatch, tmp_path):
    """P1 核心：同一轮里多个 write_file（有副作用）**串行**执行——峰值并发度必为 1。

    确定性判据（非恒真）：并发探针测 write_file 的峰值同时执行数。修复后串行 → 峰值=1；
    未修复基线把 write 丢进 8 worker 池 → 峰值>1，故本测试在基线稳定 fail。
    """
    a = Agent(max_iterations=3)
    # 预登记为已读，绕过 write_file 的先读再改约束（本测试只验并发调度）。
    from contextforge.tools import _norm
    blocks = []
    for k in range(4):
        p = tmp_path / f"f{k}.txt"
        a.read_files.add(_norm(str(p)))
        blocks.append(_FakeTUBlock("tool_use", id=f"w{k}", name="write_file",
                                   input={"path": str(p), "content": str(k)}))
    probe = _ConcurrencyProbe()
    _drive_one_round(a, blocks, monkeypatch, probe)
    assert probe.peak.get("write_file", 0) == 1, (
        f"write_file 峰值并发度={probe.peak.get('write_file')}（应为 1）—— 有副作用工具没串行，同轮写竞态未消除"
    )
    assert _unpaired_tool_uses(a.messages) == []


def test_read_only_tools_still_run_in_parallel_in_one_round(monkeypatch, tmp_path):
    """P1 不回退：同一轮里多个 read_file（只读安全）仍**并发**——峰值并发度 > 1。

    确保修复没把只读也变串行（那会白白拖慢）。read_file 标 concurrency_safe=True，应并发。
    """
    a = Agent(max_iterations=3)
    blocks = []
    for k in range(4):
        p = tmp_path / f"r{k}.txt"
        p.write_text(f"内容{k}", encoding="utf-8")
        blocks.append(_FakeTUBlock("tool_use", id=f"r{k}", name="read_file",
                                   input={"path": str(p)}))
    probe = _ConcurrencyProbe()
    _drive_one_round(a, blocks, monkeypatch, probe)
    assert probe.peak.get("read_file", 0) > 1, (
        f"read_file 峰值并发度={probe.peak.get('read_file')}（应 >1）—— 只读工具被误串行、白白变慢"
    )
