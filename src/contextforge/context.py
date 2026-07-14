"""
context.py —— 上下文管理（Memory 第一层：会话内压缩）

要解决的病：TAOR 每轮都发**完整历史**（你亲手验证过的 KV Cache 结论——发出去的
总量 = input + cache_read + cache_write）。历史越滚越长，迟早两件坏事：
  1. 质量塌：模型对中段内容召回率下降（"lost in the middle"）。
  2. 钱烧光：全量重发，即使命中 cache_read（~0.1x）也是持续开销。

压缩 vs 截断（P2 的 _truncate_for_feedback）是两层完全不同的东西：
  - 截断：治「单个」工具结果太大（一个 29 万字符的文件），砍单条 tool_result。
  - 压缩：治「多轮累积」的历史太长，调 LLM 把中段历史压成摘要。

★ "压缩 ≠ 截断 API" 的关键澄清：
  API 是**无状态**的，它只认我们这一轮发过去的 messages。所谓"压缩"，就是我们在
  本地把 self.messages 这个列表**重写**——把中间十几轮又臭又长的原文，替换成一条
  "前情摘要"消息。下轮发过去的 messages 就变短了。API 根本不知道发生过压缩，
  控制权全在我们手里。这正是"自己手搓 harness"的意义。

对照 agent_learning：第 7.2 节（上下文窗口）、第 7.4 节（上下文管理器）、
第 15.2 节（Claude Code 三级压缩）、第 8.2 节（支柱一：上下文工程）。

触发阈值（本项目决策）：
  Anthropic 官方 compaction beta 默认 150K token（≈1M 的 15%，优化质量+成本）。
  本项目是自用+学习，在知道官方理由后**主动选激进 500K**（用满一半再压）——
  想亲眼看"塞到 500K 不压会怎样"，这本身就是学习。阈值是可配的，不是物理上限。
"""


# 压缩触发阈值：当前上下文规模（真实 token）超过这个数就压缩。
# 500K = 1M 窗口的一半（本项目决策，比官方 150K 激进，见模块 docstring）。
COMPACT_THRESHOLD_TOKENS = 500_000

# 压缩时，末尾保留几「轮」原文不动（最近的对话细节还有用，不压）。
# 一「轮」= 一条 assistant 消息 + 它后面配对的 user(tool_result) 消息。
KEEP_RECENT_TURNS = 3


def current_context_tokens(usage: dict) -> int:
    """从一轮的真实 usage 算出「当前上下文规模」（这一轮实际发出去的总 token）。

    对照你亲手验证的 KV Cache 结论：发出去的总量 = input + cache_read + cache_write，
    input_tokens 只是"未缓存的新增量"，不等于发出去的总量。所以三者相加才是真实规模。

    为什么不用 tiktoken 本地估算：
      1. tiktoken 是 OpenAI 的分词器，和 Anthropic 的分词对不上，估算会偏。
      2. 真实 usage 是 API 亲口告诉我们的，最准。
    """
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )


def should_compact(usage: dict, threshold: int = COMPACT_THRESHOLD_TOKENS) -> bool:
    """判断当前上下文是否已超过压缩阈值。纯函数，方便测试。"""
    return current_context_tokens(usage) >= threshold


def _split_into_turns(middle_messages: list[dict]) -> list[list[dict]]:
    """把「中段消息」按 TAOR 的「轮」切分，保证 tool_use / tool_result 不被拆散。

    为什么必须按轮切：Anthropic 要求 assistant 的 tool_use 块，和下一条 user 消息里
    配对的 tool_result 块，必须成对出现。如果压缩时把 assistant(tool_use) 留下、
    却把它的 user(tool_result) 删了（或反之），API 会直接报错。

    切分规则：每遇到一条 assistant 消息就开一个新轮，后续的 user/其它消息归入当前轮，
    直到下一条 assistant。这样每个「轮」都是自洽的一段。
    """
    turns: list[list[dict]] = []
    for msg in middle_messages:
        if msg.get("role") == "assistant" or not turns:
            # assistant 开新轮；开头万一不是 assistant（少见）也兜底开一轮。
            turns.append([msg])
        else:
            turns[-1].append(msg)
    return turns


def _render_middle_for_summary(middle_messages: list[dict]) -> str:
    """把中段历史渲染成纯文本，喂给 summarizer 让它总结。

    messages 里的 content 可能是字符串，也可能是 content block 列表
    （TextBlock / ToolUseBlock / tool_result dict）。这里尽量把它们摊平成可读文本，
    让摘要模型能看懂"这段发生了什么"。
    """
    lines: list[str] = []
    for msg in middle_messages:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        if isinstance(content, list):
            for block in content:
                # block 可能是 SDK 对象（有 .type/.text）或普通 dict。
                btype = getattr(block, "type", None) or (
                    block.get("type") if isinstance(block, dict) else None
                )
                if btype == "text":
                    text = getattr(block, "text", None) or (
                        block.get("text") if isinstance(block, dict) else ""
                    )
                    lines.append(f"[{role}] {text}")
                elif btype == "tool_use":
                    name = getattr(block, "name", None) or (
                        block.get("name") if isinstance(block, dict) else "?"
                    )
                    tin = getattr(block, "input", None) or (
                        block.get("input") if isinstance(block, dict) else {}
                    )
                    lines.append(f"[{role}] 调用工具 {name}，参数={tin}")
                elif btype == "tool_result":
                    tc = block.get("content") if isinstance(block, dict) else ""
                    # tool_result 的 content 也可能很长，渲染时截一下（只为喂给摘要）。
                    tc_str = str(tc)
                    if len(tc_str) > 2000:
                        tc_str = tc_str[:2000] + " …(渲染截断)"
                    lines.append(f"[{role}] 工具结果：{tc_str}")
    return "\n".join(lines)


# 摘要提示词：告诉摘要模型该总结哪些维度（保留任务推进所需的关键信息）。
# P3：补第 5 维「逐字保留用户原始指令」——对照 Claude Code compact prompt「所有用户消息逐字保留、
# 最近任务逐字引用防漂移」。前 4 维是「总结/概括」（把长的压短），但用户的原话是任务的锚，一旦被
# 摘要模型改写/意译，指令语义就漂移了（如「只改登录模块」被概括成「改认证相关」，范围就变了）。
# 故明确：凡渲染里 [user] 打头的原始指令，逐字原文照抄，不得改写/概括/翻译。渲染已给每条消息打
# [role] 前缀（见 _render_middle_for_summary），摘要模型能据此识别哪些是用户原话。
_SUMMARY_PROMPT = (
    "下面是一个 AI agent 执行任务过程中的一段中间对话历史。请把它压缩成一段"
    "结构化的「前情摘要」，供 agent 继续往下做时参考。必须保留：\n"
    "1. 任务目标是什么；\n"
    "2. 已经做了哪些关键步骤（调了什么工具、改了什么文件）；\n"
    "3. 发现了哪些关键结论 / 事实 / 数据；\n"
    "4. 还没做完、下一步要往哪走；\n"
    "5. **所有用户原始指令逐字原文保留**：凡是 [user] 打头的用户原话，一字不改地照抄进摘要，"
    "不得改写、概括、意译或翻译——用户的原话是任务锚点，改写会让指令语义漂移。\n"
    "只输出摘要正文，不要客套。以下是历史：\n\n"
)


def _build_summary_prompt(directive: str | None) -> str:
    """按有无 directive 拼出摘要 prompt（T5-A 客制化）。

    directive 为空 → 逐字返回 _SUMMARY_PROMPT（向后兼容，与 P3 完全一致）。
    directive 有值 → 把用户要求作为「优先遵守的特别要求」插在四维基础要求之前，
    **叠加而非替换**：四维是底线（任务目标/已做/发现/下一步不能丢），用户要求在其上加码
    （比如"重点保留登录相关报错、其余狠删"）。
    """
    if not directive:
        return _SUMMARY_PROMPT
    return (
        f"⚠️ 本次压缩有用户指定的特别要求，请**优先遵守**：{directive}\n"
        f"在满足上述要求的前提下，仍需保留下面的基础信息。\n\n"
        + _SUMMARY_PROMPT
    )


def compact_messages(
    messages: list[dict],
    summarizer,
    keep_recent_turns: int = KEEP_RECENT_TURNS,
    directive: str | None = None,
) -> tuple[list[dict], dict] | tuple[list[dict], None]:
    """把过长的 messages 压缩：保头（原始任务）+ 压中段（LLM 摘要）+ 保尾（最近几轮原文）。

    结构：
      [原始任务]  [第1轮]...[第N-k轮]         [最近 k 轮]
        ↑保留      ↑———— 压成一条摘要 ————↑      ↑—— 保留原文 ——↑

    参数：
      messages          : 当前完整历史（会被读，不原地改）。
      summarizer        : 一个可调用对象 summarizer(text)->str，负责调 LLM 生成摘要。
                          通过参数注入，测试时可传假回调，不烧钱、不依赖网络。
      keep_recent_turns : 末尾保留几轮原文。
      directive         : T5-A 客制化——用户对本次压缩的自然语言要求（"保什么删什么"）。
                          None（默认）时 prompt 与 P3 逐字相同（向后兼容）；有值时把它作为
                          **优先遵守的特别要求**叠加在四维基础要求之上（叠加而非替换，避免
                          用户一句话就把"保留任务目标"这种底线也丢了）。

    返回：(新的 messages 列表, 压缩统计 dict)。
      若历史太短、不够压（切不出中段），返回 (原 messages, None) 表示没压。

    为什么 summarizer 用注入而非在这里直接建 client：
      让「压缩决策/切分」这些纯逻辑能被单测覆盖（不烧钱），把唯一的副作用
      （调 LLM）隔离到一个可替换的回调里。这是可测试性设计。
    """
    if not messages:
        return messages, None

    # 头：第一条消息（原始任务）必须留——否则模型忘了要干嘛。
    # ⚠️ 前提：messages[0] 恒为**纯文本 user 消息**（用户输入的任务，见 agent.py run() 里
    # 第一条就是 {"role":"user","content":task}）。因此 head 里不含 tool_use，保头不会产生
    # 「head 的 tool_use 失去配对 tool_result」的隐患。若哪天 messages[0] 变成 assistant(tool_use)
    # （非常规输入），保头+压中段会孤立该 tool_use → 下一轮 API 400；但此前提由 TAOR 结构保证，
    # 不额外加代码校验。
    head = messages[0]
    rest = messages[1:]

    # 把「头之后」的消息按轮切分。
    turns = _split_into_turns(rest)

    # 末尾保留 keep_recent_turns 轮原文，中间的才压。
    if len(turns) <= keep_recent_turns:
        # 轮数太少，压了也省不了多少，且可能把正在进行的上下文压没——不压。
        return messages, None

    middle_turns = turns[:-keep_recent_turns]
    recent_turns = turns[-keep_recent_turns:]

    # 把中段所有轮摊平成消息列表，渲染成文本喂给 summarizer。
    middle_messages = [m for turn in middle_turns for m in turn]
    if not middle_messages:
        return messages, None

    middle_text = _render_middle_for_summary(middle_messages)
    prompt = _build_summary_prompt(directive) + middle_text
    summary = summarizer(prompt)

    # 组装压缩后的历史：头 + 一条摘要消息 + 最近几轮原文。
    # 摘要用 user 角色注入（当作"系统给 agent 的前情提要"），加醒目标记便于识别。
    # 带上 directive 便于回看 trace 时知道这次压缩是带着什么目的做的。
    marker = "[前情摘要 · 由 context.py 压缩生成"
    marker += f"，按要求：{directive}]" if directive else "]"
    summary_msg = {
        "role": "user",
        "content": f"{marker}\n{summary}",
    }
    recent_messages = [m for turn in recent_turns for m in turn]
    new_messages = [head, summary_msg, *recent_messages]

    stats = {
        "before_msgs": len(messages),
        "after_msgs": len(new_messages),
        "compacted_turns": len(middle_turns),
        "kept_recent_turns": len(recent_turns),
        "summary_chars": len(summary),
    }
    return new_messages, stats


# 纯 directive 压缩提示词：与 _SUMMARY_PROMPT（叠加四维底线）不同，这条完全听用户的。
# 用于指令驱动压缩——此时用户是有意识地要按自己的方式压，不强加「必须保留任务
# 目标/下一步」等底线（保头已护住原始锚点，故这里放手让 directive 说了算）。
def _build_directive_only_prompt(directive: str) -> str:
    return (
        "下面是一个 AI agent 的对话历史片段。请严格按用户的压缩要求处理，"
        "只输出压缩后的结果正文，不要客套：\n"
        f"用户的压缩要求：{directive}\n\n以下是历史：\n\n"
    )


def compact_by_directive(
    messages: list[dict],
    summarizer,
    directive: str | None,
) -> tuple[list[dict], dict] | tuple[list[dict], None]:
    """「指令驱动压缩」——轮数不足以走结构化压缩时的降级路径（供 compact_now 调用）。

    规则：**保首条 messages[0]（最早的目标锚点）+ 按 directive 压其余 messages[1:]**，
    压缩后 = [头, 摘要]。prompt 不叠加四维基础要求（_SUMMARY_PROMPT），完全按用户
    directive（见 _build_directive_only_prompt）。

    为什么只保首条、压其余（不单独保末条）：真实 CLI 里 `/compact` 指令被拦截后直接执行、
    **不进历史**（cli.py），所以用户主动压时历史往往就是 [A问题, B回答] 两条——想压的正是 B。
    若还要「保末条」就把 B 保住了、压不动。故保住最早锚点 A、把其余（含 B、也含当前进行中的
    内容）按 directive 压成一条摘要。压缩痕迹由摘要 marker 承载（含「按要求：<directive>」，
    AI 后续读到即知这里按什么压过）。

    没有配对风险：其余整段换成一条摘要，内部 tool_use/tool_result 成对消失；保头是原始任务
    （user 纯文本），不涉及跨界配对。

    触发前提：directive 非空、`len(messages) >= 2`（至少「头 + 1 条可压」）。否则 (messages, None)。
    """
    if not directive or len(messages) < 2:
        return messages, None

    head = messages[0]
    middle = messages[1:]
    middle_text = _render_middle_for_summary(middle)
    prompt = _build_directive_only_prompt(directive) + middle_text
    summary = summarizer(prompt)

    marker = f"[前情摘要 · 由 context.py 指令驱动压缩生成，按要求：{directive}]"
    # 护栏：真实 trace 发现压缩后模型会「好心补全」被删内容（把用户明令删掉的又生成回来），
    # 违背压缩本意。故在摘要消息里明确这是用户主动删除后的最终状态、禁止补全/恢复。
    # 必须写在摘要消息本身（后续每轮主 AI 都会读到），而非生成摘要的 prompt。
    guard = "（重要：这是压缩后的最终上下文。被删内容是用户主动删除的，禁止补全、恢复或重新生成被删除的部分。）"
    summary_msg = {"role": "user", "content": f"{marker}\n{guard}\n{summary}"}
    new_messages = [head, summary_msg]

    stats = {
        "before_msgs": len(messages),
        "after_msgs": len(new_messages),
        "compacted_turns": len(middle),   # 指令驱动路径按消息计：压掉的消息数
        "kept_recent_turns": 0,           # 不单独保末条
        "summary_chars": len(summary),
    }
    return new_messages, stats
