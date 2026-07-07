"""
test_e2e.py —— 端到端测试：真调 Anthropic API，验证完整 TAOR 循环能跑通。

⚠️ 会烧少量 token。默认跑 `py -m pytest -m "not e2e"` 会跳过本文件。
   单独跑：py -m pytest -m e2e -v

为什么必须真调 API：前面的纯逻辑测试验证了「零件」（工具、截断、约束），
但「零件组装成 TAOR 循环、模型真的会调工具」只能靠真跑一次来验证。
"""

import pytest

from agent import Agent


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
