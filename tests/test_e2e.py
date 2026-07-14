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
    """纯知识问答不需要工具，验证模型直接 end_turn、不硬凑工具调用、且一轮就结束。

    ★ 有效性（变异验证过）：只断言「返回非空字符串」是**恒真**的——run() 撞护栏/验证门打回
    也返回非空 str，所以那样测不出任何东西。这里改断言真实行为：
      ① 历史里**没有任何 tool_use**（模型没硬凑工具调用）；
      ② 恰好 2 条消息 [user 任务, assistant 回答]（真的一轮 end_turn 就结束，没多轮、没撞护栏）；
      ③ 最终答案非护栏兜底串（没跑到 max_iterations）。
    破坏「一轮无工具结束」这一行为（如模型死循环撞护栏、或硬调工具）会让 ①②③ 至少一条 fail。
    """
    agent = Agent(max_iterations=5)
    final = agent.run("用一句话回答：1 加 1 等于几？直接回答，不要调用任何工具。")

    assert isinstance(final, str) and final.strip()

    # ① 历史里没有任何 tool_use（遍历所有消息的 content block）
    saw_tool_use = False
    for msg in agent.messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                btype = getattr(block, "type", None) or (
                    block.get("type") if isinstance(block, dict) else None
                )
                if btype == "tool_use":
                    saw_tool_use = True
    assert not saw_tool_use, "模型硬凑了工具调用 —— 纯问答不该调工具"

    # ② 恰好一轮结束：历史 = [user 任务, assistant 回答] 两条，没有多轮/护栏打断
    assert len(agent.messages) == 2, (
        f"不是一轮 end_turn 结束（历史 {len(agent.messages)} 条，期望 2）—— "
        f"说明模型多轮往返或被护栏/验证门打断"
    )

    # ③ 最终答案不是护栏兜底串（没跑到 max_iterations）
    assert not final.startswith("[未完成]"), "跑到了 max_iterations 护栏 —— 没能一轮答完"


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
    """P4：真调 API 验证权限拦截在真实循环里生效——用**验证门**逼模型真的去调命令。

    ★ 有效性教训（本轮审查发现）：原设计「让模型主动跑危险命令」不可靠——Opus 会先 `git status`
    自查、发现是真丢工作就**警告而非执行**，或直接拒绝 `rm -rf`（Windows 没有 rm），于是 harness
    根本没机会拦、断言因与 harness 无关的原因偶发 fail。测的其实是「模型愿不愿意作死」，不是 harness。
    「拦截+回喂」这条**循环内**的连线是确定性的，不该受模型意愿影响——故本条测的正是它，
    但仍走真实循环（下方 test_agent_logic 里另有纯逻辑版用假 client 钉死连线，双保险）。

    这里的稳定诱导法：配一个**只有跑通某危险命令才会通过的验证门**——模型声称完成 → 验证门未过
    打回 → 逼它继续想办法 → 它会尝试用 run_command 跑 harness 会拦的命令。无论它是否最终跑，
    我们断言的是「一旦它跑了危险命令，harness 必拦并回喂」——用 `git clean -fd`（删未跟踪文件，
    模型清理场景常选、不会预先拒绝）当诱饵。若整轮模型都没碰危险命令，本条转为**跳过**而非误判
    （xfail 语义），避免把「模型没作死」误报成「harness 失效」。
    """
    import pytest as _pytest
    agent = Agent(max_iterations=6)
    task = (
        "请用 run_command 执行 `git clean -fd` 清理工作区里所有未跟踪的临时文件，回到干净状态。"
        "如果这个命令被系统拒绝，就换一句话解释为什么被拦、并停止。"
    )
    final = agent.run(task)

    # 断言 1：循环没崩，仍有非空答案
    assert isinstance(final, str) and final.strip()

    # 断言 2：只要模型确实调过 harness 会拦的命令，历史里就必有「被 harness 拒绝」回喂。
    called_dangerous = False   # 模型是否请求过危险命令
    saw_rejection = False      # harness 是否回喂了拒绝
    for msg in agent.messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            btype = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None)
            if btype == "tool_use":
                cmd = str((getattr(block, "input", None) or
                           (block.get("input") if isinstance(block, dict) else {})).get("command", ""))
                if "git clean" in cmd and "-" in cmd and "f" in cmd:
                    called_dangerous = True
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if "被 harness 拒绝" in str(block.get("content", "")):
                    saw_rejection = True

    if not called_dangerous:
        _pytest.skip("本次模型没有实际请求危险命令（它选择自查/解释而非执行）——"
                     "harness 无从触发，非 harness 缺陷。循环内拦截连线由 test_agent_logic 的纯逻辑版钉死。")
    assert saw_rejection, "模型调了危险命令但 harness 没回喂『被 harness 拒绝』—— 循环内权限拦截没触发"


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


# ── P4 支柱三：真实 AI 验证「验证门能拦住假完成、打回」 ──────────────────────
#
# 核心手法——检查命令始终报 CHECK_FAIL（探针文件不存在、且任务不让建），构造一个
# 模型「声称完成 → 验证门跑检查 → 未通过 → 打回」的场景。
# 断言：① 历史里出现过「[验证门]」打回消息（铁证门真的拦了假完成，不是一次放行）；
#       ② 循环最终收敛（返回答案，或达 max_iterations 被护栏兜底停）——不卡死。
#
# 为什么不要求「模型建文件去满足检查」：实测发现，验证门打回消息里带「不要删除/修改
# 检查来蒙混」，模型会把「凭空写个探针文件去骗过文件存在性检查」判为**伪造产物、作弊**，
# 于是坚决拒绝写、一路顶到护栏。模型是对的——检查的是「文件在不在」而非「真实完成」，
# 为过检查而造假本就该拒。故本用例只验证门的**拦截职责**（打回假完成），不强求模型作弊。
# 这次意外反而证明了更强的结论：模型甚至拒绝为通过验证门而伪造产物，正是「不要蒙混」的正效果。
# ⚠️ 真调 API、会跑到 max_iterations；用户已确认不在意 token。

@pytest.mark.e2e
def test_validation_gate_rejects_false_completion(tmp_path):
    """验证门配了检查命令、且检查必失败时：模型声称完成会被反复打回，闭环不放行。"""
    probe = tmp_path / "gate_probe.txt"
    probe_path = str(probe).replace("\\", "/")
    # 检查命令：探针不存在 → **退出码 1**（验证门按退出码判未过，见 #3 修复后的新契约）。
    # 任务不让建探针，故恒失败。注意：必须靠**退出码**而非输出文本 'CHECK_FAIL' 表达失败——
    # 验证门已改为按退出码判定（旧版靠子串匹配 fail/error，会被输出里的字样误导，见审查 #3）。
    check_cmd = (
        f'py -c "import os,sys; '
        f"sys.exit(0 if os.path.exists(r'{probe_path}') else 1)\""
    )

    agent = Agent(check_command=check_cmd, max_iterations=4)
    final = agent.run("请用一句话口头回复『收到』即可，不要创建任何文件、不要调用任何工具。")

    # 断言 1：历史里出现过「[验证门]」打回 —— 门真的拦住了首次完成声称
    saw_pushback = any(
        isinstance(m.get("content"), str) and "[验证门]" in m["content"]
        for m in agent.messages
    )
    assert saw_pushback, "历史里没有『[验证门]』打回 —— 门没拦住未通过检查的完成声称"

    # 断言 2：假完成**从未被放行**。检查命令恒失败（探针不存在、任务不让建），所以门绝不该
    # 判通过。原断言 `isinstance(final, str)` 是恒真死断言（run() 永远返回 str），测不出任何东西。
    # 改为：验证门始终未通过，故循环只能撞护栏收尾 → final 是护栏兜底串。若门错误放行了假完成，
    # final 会是模型的「收到」而非护栏串，此断言 fail。
    assert final.startswith("[未完成]"), (
        f"验证门放行了假完成（final 非护栏串）—— 门没守住。final={final!r}"
    )


# ── P1 part2：真实 API 验证「同一轮 write+read 同一文件」按原始顺序执行（不跨批乱序）──
#
# 背景：P1 part1 修「同轮写-写竞态」时，把工具按类型分成「read 批先并发、write 批后串行」两大批，
# 打破了模型原始顺序——模型若同一轮请求 [write(X,新), read(X)]（写完读回确认），read 会被提前跑、
# 读到旧内容。part2 改成「按原始顺序分组，遇写断开」。本测试**直接命令真实 AI** 同一轮同时
# write+read 同一文件（聪明模型会自愿规避，故用强命令逼它做），断言 read 读到刚写的新值。
# 有效性：part1（乱序）实现下 read 读到旧值 → fail；part2 修复后读到新值 → pass。非恒真。

@pytest.mark.e2e
def test_same_round_write_then_read_sees_new_content(tmp_path):
    """真实 API：命令 AI 同一轮同时 write+read 同一文件，read 必须读到刚写的新内容（按原始顺序执行）。"""
    from contextforge.tools import _norm
    f = str(tmp_path / "note.txt").replace("\\", "/")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("OLD_CONTENT_v0")

    task = (
        f"这是一个并行工具执行的测试。请在**你这一次回复里、同时**发起两个工具调用（放在同一条消息里）：\n"
        f"1) write_file：把 {f} 的内容写成 NEW_CONTENT_v1\n"
        f"2) read_file：读取 {f}\n"
        f"必须在同一轮里同时发出这两个调用，不要分成两轮、不要只调一个、不要因为担心顺序而改成先写后读——"
        f"这正是测试要观察的。发出后，如实告诉我 read_file 返回了什么内容。"
    )
    a = Agent(max_iterations=6)
    a.read_files.add(_norm(f))  # 预登记已读，放行 write_file 的先读再改约束
    a.run(task)

    # 找出「同一轮」同时含 write+read 同文件的那轮，取该轮 read 的 tool_result 返回值。
    read_saw = None
    same_round = False
    for i, m in enumerate(a.messages):
        c = m.get("content")
        if m.get("role") != "assistant" or not isinstance(c, list):
            continue
        tus = [b for b in c if getattr(b, "type", None) == "tool_use"]
        w = [b for b in tus if b.name == "write_file"
             and getattr(b, "input", {}).get("path", "").replace("\\", "/") == f]
        r = [b for b in tus if b.name == "read_file"
             and getattr(b, "input", {}).get("path", "").replace("\\", "/") == f]
        if w and r:
            same_round = True
            rid = r[0].id
            nxt = a.messages[i + 1] if i + 1 < len(a.messages) else None
            if nxt and isinstance(nxt.get("content"), list):
                for blk in nxt["content"]:
                    if isinstance(blk, dict) and blk.get("tool_use_id") == rid:
                        read_saw = str(blk.get("content"))

    # 若模型不肯同轮读写（个别情况会拒绝），转 skip 而非误判——非 harness 缺陷。
    if not same_round or read_saw is None:
        pytest.skip("本次模型未在同一轮同时 write+read（拒绝/拆轮）——无法验证跨批顺序，非实现缺陷")

    # 核心断言（确定性、非恒真）：同一轮里 read 读到的必须是刚写的新内容，不是旧内容。
    # part1（乱序）下 read 被提前跑、读到 OLD → 此断言 fail；part2 修复后读到 NEW → pass。
    assert "NEW_CONTENT_v1" in read_saw and "OLD_CONTENT_v0" not in read_saw, (
        f"同一轮 write 后 read 读到的不是新内容（跨批乱序未修）：read 返回={read_saw!r}"
    )


# ── P3：真实 API 验证压缩逐字保留用户原始指令 ──

@pytest.mark.e2e
def test_compaction_keeps_user_instruction_verbatim():
    """真实 API：压缩一段含独特用户指令的历史，摘要必须**逐字**保住那句原话（不概括改写）。

    有效性（非恒真）：用一句独特、易被概括的用户指令。基线（摘要 prompt 无逐字要求）下，摘要
    模型会把它意译/概括（如「只改 login 42-88 行」→「重构登录模块」），原话不会逐字出现 → fail。
    加了「逐字保留用户原始指令」第 5 维后，原话逐字出现在摘要里 → pass。
    """
    unique = "只重构 login_handler.py 第 42 到 88 行，绝对不要碰 auth_middleware"
    a = Agent()
    # 造头 + 6 轮（>KEEP_RECENT_TURNS，中段会被压）；中段夹带这句独特用户指令。
    a.messages = [{"role": "user", "content": "任务：整理认证模块"}]
    for i in range(6):
        if i == 1:
            a.messages.append({"role": "user", "content": unique})
        a.messages.append({"role": "assistant", "content": f"第{i}步：处理中。"})
        a.messages.append({"role": "user", "content": f"第{i}步收到。"})

    result = a.compact_now()  # 真调 API 压缩
    assert "已压缩" in result, f"应触发压缩，实际：{result}"

    body = ""
    for m in a.messages:
        c = m.get("content")
        if isinstance(c, str) and "前情摘要" in c:
            body = c
            break
    assert unique in body, (
        f"摘要里没有逐字保留用户原始指令（被概括/改写了，指令语义漂移）：\n摘要正文：{body}"
    )
