"""
test_e2e.py —— 端到端测试：真调 Anthropic API，验证完整 TAOR 循环能跑通。

⚠️ 会烧少量 token。默认跑 `py -m pytest -m "not e2e"` 会跳过本文件。
   单独跑：py -m pytest -m e2e -v

为什么必须真调 API：前面的纯逻辑测试验证了「零件」（工具、截断、约束），
但「零件组装成 TAOR 循环、模型真的会调工具」只能靠真跑一次来验证。
"""

import pytest

from contextforge.agent import Agent


@pytest.mark.e2e
def test_agent_completes_tool_task():
    """给一个必须用工具才能答的小任务，验证：
    1. run() 返回非空最终答案；
    2. 轨迹里确实出现过 tool_use（说明模型真的调了工具、循环真的转了）。
    """
    agent = Agent(max_iterations=8)
    # 「echo」的输出模型无法凭空知道，必须调 run_command 才能回答 → 强制走工具
    final = agent.run("运行命令 echo E2E_TOKEN_OK，然后把命令的输出原样告诉我")

    # 断言 1：有非空最终答案
    assert isinstance(final, str) and final.strip()

    # 断言 2：历史里出现过 tool_use（遍历所有 assistant 消息的 content block）
    saw_tool_use = False
    for msg in agent.messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                # SDK 的 block 对象有 .type；tool_result dict 也可能在，跳过
                btype = getattr(block, "type", None) or (
                    block.get("type") if isinstance(block, dict) else None
                )
                if btype == "tool_use":
                    saw_tool_use = True
    assert saw_tool_use, "轨迹里没有 tool_use —— 模型没调工具，TAOR 没按预期走"


@pytest.mark.e2e
def test_agent_no_tool_task_ends_in_one_shot():
    """纯知识问答不需要工具，验证模型直接 end_turn、不硬凑工具调用。"""
    agent = Agent(max_iterations=5)
    final = agent.run("用一句话回答：1 加 1 等于几？直接回答，不要调用任何工具。")
    assert isinstance(final, str) and final.strip()


@pytest.mark.e2e
def test_compaction_triggers_and_task_survives():
    """P3：真调 API 验证「压缩链」端到端通。

    做法：用一个**极小的压缩阈值**（1000 token）构造 Agent。真实的 500K 阈值要塞
    巨量内容才触发（烧钱又慢），而压缩逻辑本身与阈值大小无关——用小阈值能低成本地
    真跑一次完整链路：真调 API 跑多轮 → 上下文自然超 1000 → 触发压缩 → 真调 LLM
    生成摘要 → self.messages 被重写变短 → 任务不断线、仍返回答案。

    验证：
    1. run() 返回非空最终答案（压缩后任务没断）；
    2. 历史里出现过「前情摘要」消息（说明压缩真的执行了，不是没触发）。
    """
    # 阈值压到极小（200 token）：跑几轮工具后上下文必然超过，逼出压缩。
    agent = Agent(max_iterations=12, compact_threshold=200)
    # 强制「串行多轮」：明确要求一次只跑一个命令、看到结果再跑下一个，
    # 阻止模型一轮并行做完（那样历史滚不够多轮，压缩的中段就切不出来）。
    # 需要 > KEEP_RECENT_TURNS(3) 轮，压缩才有中段可压，所以安排 5 个命令。
    task = (
        "请严格一步一步来，一次只运行一个命令，必须看到上一个命令的输出后再运行下一个，"
        "不要在同一轮里同时运行多个命令。依次用 run_command 运行："
        "第1步 echo AAA、第2步 echo BBB、第3步 echo CCC、第4步 echo DDD、第5步 echo EEE，"
        "每步都把输出告诉我，最后总结你一共运行了几个命令。"
    )
    final = agent.run(task)

    # 断言 1：压缩后任务仍完成，有非空答案
    assert isinstance(final, str) and final.strip()

    # 断言 2：历史里出现过「前情摘要」消息（压缩真的触发并重写了 messages）
    saw_summary = False
    for msg in agent.messages:
        content = msg.get("content")
        if isinstance(content, str) and "前情摘要" in content:
            saw_summary = True
            break
    assert saw_summary, "历史里没有『前情摘要』—— 压缩没触发，P3 的压缩链没跑起来"


@pytest.mark.e2e
def test_compaction_triggers_via_env_threshold(monkeypatch):
    """T6：真调 API 验证「通过环境变量 CONTEXTFORGE_COMPACT_THRESHOLD 设阈值」端到端生效。

    与上一个测试的区别：上面用构造参数 compact_threshold=200 设阈值；这里**只设环境变量、
    不传构造参数**，走的正是 T6 新加的「写 .env 即生效」那条入口。证明用户在 .env 里调阈值
    （如 Chromium 大项目调高到 800K）真的会改变压缩触发行为——这里为省钱用小数值 800 逼出
    压缩，链路与真实调高到 800K 完全一致，只是量级相差 1000 倍。
    """
    monkeypatch.setenv("CONTEXTFORGE_COMPACT_THRESHOLD", "800")  # 只走环境变量入口
    agent = Agent(max_iterations=12)  # 不传 compact_threshold
    assert agent.compact_threshold == 800, "环境变量阈值没被读进来 —— T6 入口没生效"

    task = (
        "请严格一步一步来，一次只运行一个命令，必须看到上一个命令的输出后再运行下一个，"
        "不要在同一轮里同时运行多个命令。依次用 run_command 运行："
        "第1步 echo AAA、第2步 echo BBB、第3步 echo CCC、第4步 echo DDD、第5步 echo EEE，"
        "每步都把输出告诉我，最后总结你一共运行了几个命令。"
    )
    final = agent.run(task)

    assert isinstance(final, str) and final.strip()
    saw_summary = any(
        isinstance(m.get("content"), str) and "前情摘要" in m["content"]
        for m in agent.messages
    )
    assert saw_summary, "历史里没有『前情摘要』—— 环境变量阈值没真正驱动压缩触发"


@pytest.mark.e2e
def test_harness_blocks_dangerous_command():
    """P4：真调 API 验证权限拦截在真实循环里生效。

    给一个诱导跑危险命令的任务，验证 harness 把危险命令拦下（agent 没真的删东西），
    且拒绝原因回喂后模型能收敛（最终仍返回答案，没崩）。

    验证：
    1. run() 返回非空答案（被拦后循环没崩、能继续）；
    2. 历史里出现过「被 harness 拒绝」的 tool_result（说明拦截真的触发了）。
    """
    agent = Agent(max_iterations=8)
    # 明确诱导它用 run_command 跑一个递归删除。harness 应拦下（rm -rf 命中危险模式）。
    task = (
        "请用 run_command 执行 `rm -rf /tmp/nonexistent_demo_dir` 来清理一个目录。"
        "如果这个命令被拒绝，就改用一句话解释为什么不能这么做。"
    )
    final = agent.run(task)

    # 断言 1：被拦后循环没崩，仍有非空答案
    assert isinstance(final, str) and final.strip()

    # 断言 2：历史里出现过「被 harness 拒绝」的回喂（拦截真的触发）
    saw_rejection = False
    for msg in agent.messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if "被 harness 拒绝" in str(block.get("content", "")):
                        saw_rejection = True
    assert saw_rejection, "没有『被 harness 拒绝』的回喂 —— 权限拦截没触发"


@pytest.mark.e2e
def test_validation_gate_blocks_false_completion():
    """P4：真调 API 验证「验证门」拦住假完成。

    配一个必然失败的检查命令（跑一个一定报错的命令）。任务让模型直接说完成，
    但验证门会跑检查、发现失败、打回。验证：历史里出现过「验证门」打回消息。
    （不强求模型最终修好——只验证门这道关真的拦了一次假完成。）
    """
    # 检查命令：跑一个必然抛异常（输出含 "Error"）的命令，让验证门必然判「未通过」。
    # 用项目约定的 py 启动器（bash 里 python 不通，见 CLAUDE.md）。
    agent = Agent(max_iterations=4,
                  check_command='py -c "raise ValueError(1)"')
    final = agent.run("直接回答『我已完成』，不要调用任何工具。")

    # 验证门至少打回过一次：历史里有「[验证门]」打回消息
    saw_gate_pushback = False
    for msg in agent.messages:
        content = msg.get("content")
        if isinstance(content, str) and "[验证门]" in content:
            saw_gate_pushback = True
            break
    assert saw_gate_pushback, "验证门没打回假完成 —— 验证门没生效"
    assert isinstance(final, str)


@pytest.mark.e2e
def test_main_agent_spawns_subagent():
    """P5：真调 API 验证主 agent 能派生子 agent、子 agent 独立跑完、只回传结论。

    给一个明确适合外包的任务，验证：
    1. 主 agent 真的调了 spawn_subagent（历史里有该 tool_use）；
    2. 历史里出现过「[子 agent 完成]」的回喂（子 agent 真跑完并回传了结论）；
    3. 最终有非空答案（主 agent 拿到子 agent 结果后收尾）。

    上下文隔离的体现：主 agent 的历史里只有子 agent 的**结论**（一条 tool_result），
    看不到子 agent 读文件、试错的中间过程 —— 那些留在子 agent 自己的 messages 里。
    """
    agent = Agent(max_iterations=8)
    # 明确引导它外包：强调"用 spawn_subagent 派一个子 agent 去做，你只收结论"。
    task = (
        "请使用 spawn_subagent 工具，派一个子 agent 去执行命令 `echo SUBAGENT_RESULT_42` "
        "并把该命令的输出取回来。你自己不要直接跑这个命令，务必交给子 agent。"
        "拿到子 agent 的结论后，把其中的输出值告诉我。"
    )
    final = agent.run(task)

    # 断言 1：主 agent 历史里出现过 spawn_subagent 的 tool_use
    saw_spawn = False
    for msg in agent.messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                btype = getattr(block, "type", None) or (
                    block.get("type") if isinstance(block, dict) else None
                )
                bname = getattr(block, "name", None) or (
                    block.get("name") if isinstance(block, dict) else None
                )
                if btype == "tool_use" and bname == "spawn_subagent":
                    saw_spawn = True
    assert saw_spawn, "主 agent 没调用 spawn_subagent —— 没派生子 agent"

    # 断言 2：历史里有「[子 agent 完成]」回喂（子 agent 真跑完回传了结论）
    saw_sub_result = False
    for msg in agent.messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if "[子 agent 完成]" in str(block.get("content", "")):
                        saw_sub_result = True
    assert saw_sub_result, "历史里没有『[子 agent 完成]』—— 子 agent 结论没回传"

    # 断言 3：最终有非空答案
    assert isinstance(final, str) and final.strip()


# ── T5-A：真实 AI 验证 directive 客制化压缩真的被遵守 ────────────────────
#
# 设计（与用户对齐，题材直接对准用户踩过的真实故障）：
#   用户遇到的坑——AI 每次调工具都带一句重复的「口癖」（如"让我来运行…"），这句口癖
#   在历史里一轮轮堆积，垃圾文本正反馈，最后 AI 被自己的口癖淹没、无法再调工具。
#   compact 的真实价值就是把这类**重复过程口癖/噪声**删掉、只留真实进展——本测试正是
#   验证这一点。
#
# 做法：直接 seed 一段受控历史（不靠模型串行攒，脆且多轮 API）：
#   - 每轮都夹一句**固定重复的口癖** _TIC（= 要删的噪声 B）；
#   - 夹带真正的进展信息，带独特标记 _PROGRESS（= 要留的 A）。
#   然后 compact_now(directive=...) 只发一次真实 API 调用（summarizer），断言两个方向：
#   方向一（保留 A）：directive 说聚焦进展 → 摘要含 _PROGRESS，且真的压缩了（变短）。
#   方向二（降频 B）：口癖堆积很多次 → directive 要求降频 → 摘要里那句口癖至多剩 1 次。
#
# 两个关键设计：
#   1. 方向二用**"降频/去重"语义**（"重复很多次…至多保留一次"），不用"删除/隐藏/绝不出现"
#      这种强硬措辞——后者的意图形状像"清理/隐藏记录"，会触发模型安全拒答（T5-A 实操踩过，
#      见 TODO T5-B）。断言也用**次数对比**（从 ≥6 次降到 ≤1 次），比"绝对为 0"更真实、更稳，
#      也正好匹配真实故障：口癖是"堆积"成灾，压缩是"降频"而非"抹净"。
#   2. 断言只查摘要**正文**（标记行之后），不查标记行——因为标记行会把整个 directive 抄进去，
#      而 directive 里就含口癖原文，若连标记行一起查会污染计数。
# ⚠️ LLM 非确定性：极小概率模型不完全遵守 → 偶发红；属 e2e、默认不跑，可接受。

_PROGRESS = "PROGRESS_STEP_DBSCHEMA"   # 要保留的真实进展（A）
# 要降频的重复口癖（B）：用真实故障里那种自然语言口癖（模型每次调工具带的那句话），
# 而非机械 token——贴合用户遇到的"口癖一轮轮堆积、正反馈淹没 agent"的真实场景。
_TIC = "让我继续按部就班地把这个任务往前推进"


def _seed_history_with_tic() -> list[dict]:
    """造头 + 6 轮历史：每轮 assistant 都带同一句重复口癖 _TIC；奇数轮夹带真实进展 _PROGRESS。

    6 轮 > 保留最近 3 轮，中段（前 3 轮）会被压——前 3 轮里既有反复的口癖，也有真实进展，
    正好考验 summarizer 按 directive 取舍：删掉重复口癖、留住进展。纯文本一问一答，结构最简。
    """
    msgs: list[dict] = [{"role": "user", "content": "任务：整理这批数据表的字段"}]
    for i in range(6):
        # 每轮 assistant 都以同一句口癖开头（模拟"每次调工具都带的那句话"堆积）。
        assistant_line = f"{_TIC}。第{i}步处理中。"
        if i % 2 == 1:
            assistant_line += f"（进展：完成了 {_PROGRESS} 的整理。）"
        msgs.append({"role": "assistant", "content": assistant_line})
        msgs.append({"role": "user", "content": f"第{i}步收到，继续。"})
    return msgs


def _extract_summary_body(messages: list[dict]) -> str:
    """取出「前情摘要」消息的**正文**（去掉第一行标记，因标记会抄入整个 directive）。"""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str) and content.startswith("[前情摘要"):
            # 标记在第一行，正文从第二行起——只对正文做断言，不受标记里 directive 文本干扰。
            parts = content.split("\n", 1)
            return parts[1] if len(parts) > 1 else ""
    return ""


@pytest.mark.e2e
def test_compact_directive_keeps_requested_content():
    """方向一（保留 A）：directive 说聚焦真实进展 → 摘要正文含进展标记，且真的压缩了。"""
    agent = Agent()
    agent.messages = _seed_history_with_tic()
    before_len = len(agent.messages)

    result = agent.compact_now(
        directive=f"摘要请聚焦真实的处理进展（如 {_PROGRESS} 这类完成项），完整保留。"
    )

    # 断言 1：确实压缩了（消息变短 + 返回"已压缩"）
    assert "已压缩" in result
    assert len(agent.messages) < before_len

    # 断言 2：directive 点名要留的进展标记，真的出现在摘要正文里
    body = _extract_summary_body(agent.messages)
    assert body, "没找到『前情摘要』正文 —— 压缩没按预期重写历史"
    assert _PROGRESS in body, (
        f"摘要正文里没有 {_PROGRESS} —— 真实 AI 没有遵守『保留进展』指令。正文：\n{body}"
    )


@pytest.mark.e2e
def test_compact_directive_drops_repeated_tic():
    """方向二（降频 B）：口癖在历史里堆积很多次 → directive 要求降频 → 摘要里至多剩 1 次。

    这正是用户踩过的坑的正解：同一句口癖一轮轮堆积成正反馈（真实故障里 AI 被自己的口癖
    淹没、无法再调工具），compact 的价值就是把 N 次降到 1 次以下。用"降频/去重"语义而非
    "删除/隐藏"，既贴合真实场景，又不触发模型安全拒答（T5-A 实操踩过，见 TODO T5-B）。
    """
    agent = Agent()
    agent.messages = _seed_history_with_tic()
    # 原始历史里口癖出现了多少次（6 轮每轮一次，≥6）——这就是"堆积"。
    before_count = sum(
        str(m.get("content", "")).count(_TIC) for m in agent.messages
    )

    agent.compact_now(
        directive=(
            f"历史里这句口头禅「{_TIC}」重复出现了很多次，是无信息的过程噪声。"
            f"压缩后这句话最多只保留一次，其余重复的都去掉，摘要聚焦真实进展。"
        )
    )

    # 断言：口癖从"堆积很多次"降到"至多 1 次"，且确实比原来少（真的降频了）。
    body = _extract_summary_body(agent.messages)
    assert body, "没找到『前情摘要』正文 —— 压缩没按预期重写历史"
    after_count = body.count(_TIC)
    assert before_count >= 6, f"seed 历史口癖没堆够（{before_count} 次），测试前提不成立"
    assert after_count <= 1, (
        f"口癖降频失败：摘要正文里仍有 {after_count} 次「{_TIC}」（原 {before_count} 次）。"
        f"真实 AI 没把堆积的口癖压下去。正文：\n{body}"
    )
    assert after_count < before_count, "口癖次数没减少 —— 压缩没起到去重降频作用"


# ── T5-A：真实 AI 验证 subagent 执行者「真的回读文件核实」而非盲总结 ──────────
#
# 核心手法——制造「历史说的」和「文件真实的」之间的**分歧**：
#   - 在真实文件里写一个当前值（CURRENT）。
#   - seed 历史里故意说成一个**过期的旧值**（STALE，和 CURRENT 不同）。
#   - directive 让压缩时回读文件核实这个值。
# 然后断言摘要里出现 CURRENT（真实值）——盲总结（_summarize）只有历史里的 STALE 可抄、
# 绝不可能写出 CURRENT，所以 CURRENT 一旦出现，就铁证子 agent 真的 read_file 核实了。
# （不断言 STALE 缺席：子 agent 为说明"值已更新"提一嘴旧值是合理表述，见下方断言注释。）
# ⚠️ 慢（子 agent 跑多轮 TAOR）+ 真调 API；用户已确认这条不在意 token。

_PROBE_CURRENT = "CURRENT_VALUE_9911"   # 文件里真实的当前值
_PROBE_STALE = "STALE_VALUE_1234"       # 历史里过期的旧值（故意与当前不符）


@pytest.mark.e2e
def test_subagent_executor_reverifies_by_reading_file(tmp_path):
    """subagent 执行者会真的 read_file 核实：摘要写的是文件真实值，而非历史里的过期值。"""
    probe = tmp_path / "probe.txt"
    probe.write_text(f"探针文件当前内容：{_PROBE_CURRENT}", encoding="utf-8")

    agent = Agent(compact_executor="subagent")
    # seed 头 + 6 轮历史：反复"声称"探针值是 STALE（过期），并给出文件路径供核实。
    agent.messages = [{"role": "user", "content": f"任务：核对探针文件 {probe}"}]
    for i in range(6):
        agent.messages.append({
            "role": "assistant",
            "content": f"第{i}步：（历史记录，可能已过期）探针文件 {probe} 的值是 {_PROBE_STALE}。",
        })
        agent.messages.append({"role": "user", "content": f"第{i}步收到。"})

    result = agent.compact_now(
        directive=(
            f"历史里说探针文件 {probe} 的值是 {_PROBE_STALE}，但这可能已过期。"
            f"请用 read_file 回读该文件，把**文件当前真实的值**写进摘要。"
        )
    )

    assert "已压缩" in result
    body = _extract_summary_body(agent.messages)
    assert body, "没找到『前情摘要』正文 —— 压缩没按预期重写历史"
    # 关键断言：摘要含文件真实值 CURRENT —— 盲总结只有历史里的 STALE 可抄、绝不可能写出
    # CURRENT，所以 CURRENT 一旦出现，就铁证子 agent 真的 read_file 回读核实了。
    # 不断言 STALE 缺席：子 agent 为说明"值已更新"而提一嘴旧值（"STALE 已过期，现为 CURRENT"）
    # 是完全合理甚至更好的表述，苛求旧值一个字不出现反而是错的判据。
    assert _PROBE_CURRENT in body, (
        f"摘要里没有文件真实值 {_PROBE_CURRENT} —— 子 agent 没有回读核实（盲总结只会照抄"
        f"历史里的过期值 {_PROBE_STALE}）。正文：\n{body}"
    )
