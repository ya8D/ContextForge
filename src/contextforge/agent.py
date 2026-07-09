"""
agent.py —— 核心 TAOR 循环（Think → Act → Observe → Repeat）

这是整个项目的心脏。一个 agent 的本质，就是「一个带工具的 while 循环」：
  Think   : 调 LLM，模型决定「说话」还是「调工具」
  Act     : 如果模型要调工具，我们的代码去执行
  Observe : 把工具结果包成 tool_result，追加进历史
  Repeat  : 回到 Think，直到模型不再要工具（stop_reason == end_turn）

对照 agent_learning：第 1.3 节（OTA 循环）、第 3.2 节（Function Calling）、
第 15.2 节（Claude Code 的 TAOR）。

Anthropic 消息形态（与书里 OpenAI 版的关键差异，见 CLAUDE.md 对照表）：
- 模型请求工具 → content 里的 tool_use block（含 id / name / input）
- 回喂结果     → role:"user" 消息里放 tool_result block（靠 tool_use_id 配对）
- 是否要工具   → resp.stop_reason == "tool_use"
"""

import itertools
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from contextforge.context import (
    COMPACT_THRESHOLD_TOKENS,
    compact_by_directive,
    compact_messages,
    current_context_tokens,
    should_compact,
)
from contextforge.harness import LoopDetector, ValidationGate, check_tool_call
from contextforge.tools import TOOL_SCHEMAS, execute_tool, subagent_tool_schemas, tool

# 加载项目根的 .env（代理凭据：ANTHROPIC_AUTH_TOKEN / BASE_URL / MODEL）。
# 这套凭据由 VSCode 扩展注入其子进程，普通终端拿不到，故落到本地 .env。
# 本文件位于 src/contextforge/ 下，.env 和 traces/ 实际在项目根，
# 故要上跳两级（src/contextforge/ → src/ → 项目根），而非本文件所在目录。
_HERE = Path(os.path.abspath(__file__)).parent.parent.parent
load_dotenv(_HERE / ".env")

# trace 落盘根目录（已在 .gitignore 中忽略）。
_TRACES_ROOT = _HERE / "traces"

# 回喂给模型的单个工具结果字符上限（Observe 加厚 / 防上下文爆炸）。
# 超出则截断并提示模型分段读取。对照 run_20260706_220338 turn_04 的暴涨教训。
# 1M 大窗口下 8000 太抠（P3 决策：调到 5 万，单个大文件也能大体读全，
# 真正的多轮累积膨胀交给 context.py 的压缩层处理，两层各管各的）。
_MAX_RESULT_CHARS = 50000


# ── 轻量 trace 工具：让 TAOR 每一步透明可见（对照第 18.5 节 可观测性）──
# 用带颜色/图标的前缀区分阶段，肉眼一眼就能跟上循环在做什么。
# T1：CONTEXTFORGE_LOG 控屏幕分级（off/normal/debug，默认 normal）；level="error" 的调用点
# （权限拦截/死循环/验证门/护栏）即使 off 档也照打——用户仍需第一时间看到问题。
# 每次调用都读一次环境变量（不缓存），换取实现简单、测试好控制，性能代价可忽略。
def _log(tag: str, msg: str, level: str = "normal") -> None:
    setting = os.environ.get("CONTEXTFORGE_LOG", "normal").strip().lower()
    if setting not in ("off", "normal", "debug"):
        setting = "normal"  # 非法值兜底为默认档，不报错、不吞正常输出
    if level == "error":
        print(f"[CF] {tag} {msg}")
        return
    if setting == "off":
        return
    if level == "debug" and setting != "debug":
        return
    print(f"[CF] {tag} {msg}")


def _trace_enabled() -> bool:
    """CONTEXTFORGE_TRACE 是否开启（默认 on）。独立于 CONTEXTFORGE_LOG，控制 traces/ 落盘。"""
    return os.environ.get("CONTEXTFORGE_TRACE", "on").strip().lower() != "off"


def _resolve_compact_threshold(explicit: int | None) -> int:
    """解析压缩触发阈值。优先级：显式传参 > CONTEXTFORGE_COMPACT_THRESHOLD > 默认 500K。

    环境变量非法（非正整数）时兜底回默认，不报错——配置写错不该让 agent 起不来。
    Chromium 这类大项目可把阈值调高，用满更多上下文再触发压缩。
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get("CONTEXTFORGE_COMPACT_THRESHOLD")
    if raw:
        try:
            val = int(raw.strip())
            if val > 0:
                return val
        except ValueError:
            pass  # 非法值兜底回默认
    return COMPACT_THRESHOLD_TOKENS


def _to_serializable(obj):
    """把 messages 里的内容转成可 JSON 序列化的形式。

    messages 里混着两种东西：我们自己写的普通 dict，和 SDK 返回的
    content block 对象（如 TextBlock / ToolUseBlock）。后者不是 dict，
    但都有 .model_dump() 能转成 dict。这里递归处理，让整份 messages 能落盘。
    """
    if isinstance(obj, list):
        return [_to_serializable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if hasattr(obj, "model_dump"):  # anthropic SDK 的 Pydantic 对象
        return obj.model_dump()
    return obj


def _truncate_for_feedback(result: str) -> str:
    """回喂给模型前，把超长工具结果截断，防止上下文爆炸（Observe 加厚）。

    只截断「回喂给模型」的内容，磁盘/真实数据不受影响。超出上限时保留头部，
    并明确提示模型「已截断、可分段读取」，给它一个自我纠正的方向。
    """
    if len(result) <= _MAX_RESULT_CHARS:
        return result
    head = result[:_MAX_RESULT_CHARS]
    return (f"{head}\n\n[⚠️ 结果过长已截断：原文共 {len(result)} 字符，"
            f"仅回喂前 {_MAX_RESULT_CHARS} 字符。如需完整内容，请分段读取"
            f"（如指定行范围、或用命令过滤 grep/head）。]")


class Agent:
    """最小 TAOR agent：一个循环 + 一组工具 + 一段对话历史。"""

    # 进程内单调递增的实例序号，给 trace 目录做**唯一后缀**。
    # 为什么不用 id(self)：短生命周期的 Agent 建完即被回收，id() 会被下一个实例复用 →
    # 同秒并行/连续创建时低位撞车、trace 目录仍会撞名（实测 5 个实例常只有 1-2 个唯一）。
    # 单调计数器进程内绝不重复，且零随机性（符合项目「不用 random/uuid」的风格）。
    _instance_seq = itertools.count()

    def __init__(self, model: str | None = None, max_iterations: int = 100,
                 compact_threshold: int | None = None,
                 check_command: str | None = None,
                 tools: list | None = None,
                 compact_directive: str | None = None,
                 compact_executor: str | None = None):
        # 模型 ID 从环境读，不写死（见 CLAUDE.md）。
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8[1m]")
        # SDK 自动读取 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL。
        self.client = anthropic.Anthropic()
        # 硬护栏：循环最多转几轮，防止失控无限循环烧 token（第 1.3 节 护栏）。
        self.max_iterations = max_iterations
        # 上下文压缩阈值（真实 token）。优先级：显式传参 > 环境变量 CONTEXTFORGE_COMPACT_THRESHOLD
        # （写 .env 持久生效，Chromium 这类大项目可调高以用满更多上下文再压）> 默认 500K。
        # 测试可传小值以便低成本触发压缩、验证「压缩链真的通」而不必真塞 500K。
        self.compact_threshold = _resolve_compact_threshold(compact_threshold)
        # 可用工具集（P5 上下文隔离用）。默认 None = 用全局全集 TOOL_SCHEMAS（主 agent）。
        # 子 agent 会被传入一个**受限子集**（不含 spawn_subagent），防止无限递归派生。
        self.tool_schemas = tools if tools is not None else TOOL_SCHEMAS
        # ── T5-A 客制化 compact ──
        # 会话级压缩偏好：被动压缩（到阈值自动触发）默认带上它。
        # 优先级：显式传参 > 环境变量 CONTEXTFORGE_COMPACT_DIRECTIVE（写 .env 持久生效）> None（默认四维）。
        # 与 self.model 读 ANTHROPIC_MODEL 同款兜底思路，无需给 CLI 加新命令。
        self.compact_directive = compact_directive or os.environ.get("CONTEXTFORGE_COMPACT_DIRECTIVE") or None
        # 压缩执行者："self"（默认，_summarize 盲总结）或 "subagent"（派带工具的子 agent 回读核实）。
        # 优先级：显式传参 > 环境变量 CONTEXTFORGE_COMPACT_EXECUTOR > "self"（默认）。
        self.compact_executor = (
            compact_executor or os.environ.get("CONTEXTFORGE_COMPACT_EXECUTOR") or "self"
        ).strip().lower()
        # ── Harness 约束（P4，对照第 8 章三根柱子）──
        # ② 死循环检测：连续 3 次相同 action 就判定鬼打墙、注入换思路提示。
        self.loop_detector = LoopDetector(max_same=3)
        # ③ 验证门：若任务配了检查命令（如 pytest），声称完成前强制跑一遍。
        # 优先级：显式传参 > 环境变量 CONTEXTFORGE_CHECK_COMMAND（写 .env 持久生效）> None（跳过）。
        # 与 compact_directive 读 CONTEXTFORGE_COMPACT_DIRECTIVE 同款兜底。存一份到实例，
        # 供 CLI /check 查看/复用（门本身的 check_command 是构造时定的只读字段）。
        self.check_command = check_command or os.environ.get("CONTEXTFORGE_CHECK_COMMAND") or None
        self.validation_gate = ValidationGate(check_command=self.check_command)
        # 对话历史：TAOR 每一轮的「所见所想所做」都累积在这里。
        self.messages: list[dict] = []
        # 「本会话已读文件」集合——「先读再改」约束的状态。**每个 Agent 实例各一套**（含子 agent），
        # 由 execute_tool 带外注入给 read_file/write_file。放实例而非模块全局，才能让 reset（新建实例）
        # 清空它、子 agent 与主 agent 天然隔离、并行执行时各写各的不竞态。
        self.read_files: set = set()
        # 本次会话的 trace 根目录，按 年/月/日/run_时分秒_序号 分层（普通脚本可用 datetime.now）。
        # 分层便于按日期归档。末尾的实例序号是**唯一标识**：一轮里可并行派生多个子 agent
        # （spawn_subagent / _summarize_via_subagent），它们同一秒创建，若只到秒级会撞同一个
        # run_HHMMSS/ 目录、各自 task_01 互相覆盖、静默丢 trace。用进程内单调序号区分（见 _instance_seq）。
        # 一个会话（一个实例）可跑多个任务，每个任务再单独建 task_NN 子目录。
        _now = datetime.now()
        self.trace_dir = (
            _TRACES_ROOT / f"{_now:%Y}" / f"{_now:%m}" / f"{_now:%d}"
            / f"run_{_now:%H%M%S}_{next(Agent._instance_seq):04d}"
        )
        if _trace_enabled():
            self.trace_dir.mkdir(parents=True, exist_ok=True)
        # 跨 run() 只增不减的任务计数器（第几个任务）。
        self.task_counter = 0

    def run(self, task: str) -> str:
        """跑一个任务，返回模型的最终文字答案。"""
        # 每个任务单独建一个 task_NN 子目录，避免跨任务的 turn 文件互相覆盖。
        self.task_counter += 1
        self.current_task_dir = self.trace_dir / f"task_{self.task_counter:02d}"
        if _trace_enabled():
            self.current_task_dir.mkdir(parents=True, exist_ok=True)

        # 死循环检测是**任务内**概念：清空上个任务残留的 action 指纹，别让任务2 头几轮和任务1
        # 结尾混在同一滑动窗口里误判/漏判。（区别于有意跨任务保留的 messages / validation_gate——
        # 那是「短期记忆」；而循环窗口跨任务保留没有语义依据。）
        self.loop_detector.reset()

        _log("\n🎯 [任务]", task)
        if _trace_enabled():
            _log("📁 [trace]", f"本任务 in/out 落盘到 {self.current_task_dir}")
        # 用户任务作为历史的第一条消息。
        self.messages.append({"role": "user", "content": task})

        for i in range(1, self.max_iterations + 1):
            _log(f"\n🔄 ===== TAOR 第 {i}/{self.max_iterations} 轮 =====", "")

            # 快照「这一轮实际发给 API 的 messages」——就是当前 self.messages。
            # 每轮都发完整历史；落盘后可对比多轮 messages 证明这一点。
            messages_sent = _to_serializable(self.messages)

            # ── Think：调 LLM，让模型决定下一步 ──
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                tools=self.tool_schemas,     # 本 agent 可用的工具（主=全集，子=受限子集）
                messages=self.messages,
            )

            # 取出 usage 的 4 个关键字段（cache 字段可能不存在，用 getattr 兜底）。
            u = response.usage
            usage = {
                "input_tokens": u.input_tokens,                                   # 未缓存的新增量（付全价）
                "output_tokens": u.output_tokens,                                 # 本轮生成
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),  # 写入缓存
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),          # 从缓存读（≈0.1 价）
            }

            # 落盘：这一轮的完整 in（messages_sent）+ 本轮模型输出（response.content）+ usage。
            self._dump_turn(i, messages_sent, usage, response.stop_reason, response.content)

            # 打印一行缓存对比汇总，一眼看清「input 只是新增量，其余命中缓存」。
            cr = usage["cache_read_input_tokens"]
            cw = usage["cache_creation_input_tokens"]
            _log("🧠 [Think]", f"stop_reason={response.stop_reason}  "
                              f"in={usage['input_tokens']} "
                              f"(cache_read={cr}, cache_write={cw}) / "
                              f"out={usage['output_tokens']}")
            # debug 档专属：逐轮追踪上下文规模逼近压缩阈值的过程（不用等压缩真触发才看到数字）。
            _log("📊 [debug]",
                 f"当前上下文规模 {current_context_tokens(usage)} / 压缩阈值 {self.compact_threshold}",
                 level="debug")

            # 把模型这一轮的输出（可能含文字 + 工具请求）原样存回历史。
            self.messages.append({"role": "assistant", "content": response.content})

            # 打印模型说的话（如果有文字块）。
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    _log("💬 [模型]", block.text.strip())

            # ── 判断：模型是否要调工具？ ──
            if response.stop_reason != "tool_use":
                # 模型不要工具了 —— 但先别急着放它走。
                # ③ 验证门（对照第 8.2 支柱三，成功率提升最大的一项）：
                # 若任务配了检查命令，声称完成前强制跑一遍，过了才算真完成。
                final = "".join(b.text for b in response.content if b.type == "text")
                passed, report = self.validation_gate.verify(self._run_check)
                if passed:
                    _log("\n✅ [完成]", "模型未再请求工具，且通过验证门，循环结束。")
                    return final
                # 没过验证门 → 把失败报告当 user 消息打回去，让模型继续修，不放行。
                _log("\n🚧 [验证门]", "未通过，打回让模型继续修复。", level="error")
                self.messages.append({
                    "role": "user",
                    "content": (f"[验证门] 你声称完成了，但强制检查未通过。"
                                f"不要删除或修改检查/测试来蒙混，请修复真正的问题后继续。\n{report}"),
                })
                continue  # 回到 Think，让模型据此继续

            # ── Act + Observe：先过 harness 关卡，再并行执行 ──
            # 先挑出这一轮所有的 tool_use 块（模型可能一轮请求多个）。
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # ② 死循环检测（对照第 8.2 支柱三配套）：把**整轮**所有工具记为一个指纹，
            # 连续 3 轮完全相同 → 判定鬼打墙，注入「换思路」提示打断，不再执行。
            # 用整轮（而非只取第一个工具）避免两种误判：多工具乱序的真循环漏报、
            # 第一个工具恰好相同但整轮在推进的误报（详见 harness.record_round 注释）。
            # 注意：assistant 消息已在上面（Think 之后）统一 append 过，这里不再重复。
            # 不在命中后 reset：若模型不听劝继续重复，下一轮应**再次**触发（一次不漏），
            # 而非清零后放它再跑 max_same-1 轮；换了动作滑动窗口自会放行，无需 reset。
            # 最坏情况由 max_iterations 兜底。
            self.loop_detector.record_round(tool_use_blocks)
            if self.loop_detector.is_looping():
                _log("\n🔁 [死循环]", "连续多轮相同 action，注入换思路提示、打断。", level="error")
                self.messages.append({
                    "role": "user",
                    "content": ("[死循环检测] 你连续多次重复了完全相同的操作但没有进展。"
                                "请停下来换一个思路：换个命令/参数、或换个角度分析问题，"
                                "不要再重复刚才那个动作。"),
                })
                continue  # 回到 Think

            # ① 权限关卡（对照第 8.2 支柱二）：执行前逐个过 check_tool_call。
            # 命中危险（rm -rf / 路径遍历等）→ 不执行，把拒绝原因当结果回喂让模型换做法。
            # 用代码强制，不靠模型自律 —— 这是本项目的 harness 核心。
            # 先算好每个工具「放行/拒绝」，放行的才真正并行执行。
            gate_results: list[tuple] = []  # (block, 是否放行, 拒绝原因或 None)
            to_execute = []                  # 放行的 block（送去并行执行）
            for block in tool_use_blocks:
                ok, reason = check_tool_call(block.name, block.input)
                gate_results.append((block, ok, None if ok else reason))
                if ok:
                    to_execute.append(block)
                else:
                    _log("🛡️ [权限]", f"拦截 {block.name} 参数={block.input} —— {reason}", level="error")

            # 并行执行放行的工具（对照第 15.2 节 并行工具调用）。
            executed: dict = {}  # block.id -> 执行结果
            if to_execute:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(
                        # read_files 带外注入本实例的「已读集合」——read_file/write_file 收到它，
                        # 别的工具忽略。每个 Agent 各写各的 set，并行不竞态、子 agent 不泄漏。
                        lambda b: execute_tool(b.name, b.input, read_files=self.read_files),
                        to_execute,
                    ))
                for b, r in zip(to_execute, results):
                    executed[b.id] = r

            # 组装每个 tool_use 的结果：放行的用真实执行结果，被拦的用拒绝说明。
            tool_results = []
            for block, ok, reason in gate_results:
                if ok:
                    result = executed[block.id]
                    _log("🦾 [Act]", f"调用工具 {block.name}  参数={block.input}")
                    preview = result if len(result) <= 300 else result[:300] + " …(打印截断)"
                    _log("👀 [Observe]", preview)
                    content = _truncate_for_feedback(result)
                else:
                    # 被 harness 拦下：把拒绝原因作为结果回喂（is_error 标记让模型知道这是错误）。
                    content = f"[被 harness 拒绝] {reason}。请改用更安全的做法。"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })

            # 所有工具结果作为一条 user 消息回喂 → Repeat（回到 Think）
            self.messages.append({"role": "user", "content": tool_results})

            # ── 上下文压缩（P3）：用本轮真实 usage 判断规模，超阈值就压 ──
            # 放在回喂之后、进下一轮 Think 之前：这样下一轮发出去的就是压缩后的历史。
            # 刚追加的 assistant 输出 + tool_result 属于"最近几轮"，压缩会保留原文，不受影响。
            self._maybe_compact(usage)

        # 达到最大轮数还没结束 —— 护栏触发。
        _log("\n⛔ [护栏]", f"达到最大轮数 {self.max_iterations}，强制停止。", level="error")
        return "[未完成] 达到最大迭代轮数。"

    def _dump_turn(self, turn: int, messages_sent, usage, stop_reason,
                   response_content=None) -> None:
        """把这一轮的完整 in/out 落盘成一个 JSON 文件，供实地调查。

        response_content：本轮模型的输出（response.content，SDK content block 列表）。
        为什么要它：messages_sent 是"调 LLM 之前"的快照，只含发出去的输入；模型回复要到
        下一轮才作为历史出现，若本轮 end_turn 就结束则任何 trace 都不落其文字内容（只有
        usage.output_tokens 这个量化值）。把 response_content 单独存进来，trace 才能完整
        复盘"模型这轮说了什么"。默认 None 向后兼容。
        """
        if not _trace_enabled():
            return  # CONTEXTFORGE_TRACE=off：不落盘（屏幕日志仍受 CONTEXTFORGE_LOG 独立控制）。
        # 写进当前任务的子目录，turn 是「本任务内」的轮次，跨任务不会撞名。
        path = self.current_task_dir / f"turn_{turn:02d}.json"
        payload = {
            "turn": turn,
            "model": self.model,
            "stop_reason": stop_reason,
            "usage": usage,
            "messages_sent": messages_sent,          # 这一轮实际发给 API 的完整历史（输入侧）
            "response_content": _to_serializable(response_content),  # 本轮模型输出（输出侧）
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _summarize(self, prompt: str) -> str:
        """summarizer 回调：调一次 LLM 把中段历史压成摘要。

        这是压缩层唯一的副作用。单独一次「无工具、无历史」的调用——
        只把要总结的文本作为一条 user 消息发过去，拿回摘要文字。
        不带 tools、不带 self.messages，是一次干净的一次性调用。
        """
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    def _run_check(self, command: str) -> str:
        """验证门的 runner 回调：跑检查命令（如 pytest）拿输出。

        复用 tools.run_command 执行——同 summarizer 注入思路：ValidationGate 只懂
        「验证的判据」，具体怎么跑命令由这个注入的回调完成，逻辑与副作用分离。
        """
        from contextforge.tools import run_command
        return run_command(command)

    def _summarize_via_subagent(self, prompt: str) -> str:
        """T5-A 执行者「subagent」：派一个带工具的子 agent 去做压缩摘要。

        与 _summarize（盲总结一次）的区别：子 agent 有独立上下文 + 受限工具集
        （read/run/write），能**回读原始文件核实**某个结论是否还成立，而非凭记忆总结。
        这就是「压缩执行者可切换成子 agent」的增量价值（复用 P5 sub-agent 机制）。
        子 agent 的中间过程留在它自己的 messages 里，只回传最终摘要。
        """
        sub = Agent(
            tools=subagent_tool_schemas(),
            max_iterations=15,
        )
        task = (
            "你是一个上下文压缩助手。下面给你一段 agent 的中间对话历史和压缩要求。"
            "请产出符合要求的『前情摘要』。如果历史里提到某个文件/命令的结论，你可以用"
            "read_file / run_command **回读核实**它现在是否还成立，再据实写进摘要。"
            "只输出摘要正文。\n\n" + prompt
        )
        return sub.run(task)

    def _pick_summarizer(self):
        """按 self.compact_executor 选压缩执行者回调（self=盲总结 / subagent=带工具核实）。"""
        return self._summarize_via_subagent if self.compact_executor == "subagent" else self._summarize

    def compact_now(self, directive: str | None = None) -> str:
        """主动压缩入口（供 CLI 的 /compact 调用）：用户说压就压，不看 usage 阈值。

        directive：用户当场输入的压缩要求；None 时回退到会话级 self.compact_directive。
        返回一句人类可读的结果行。够轮数才压，不够则如实说未压。
        """
        directive = directive or self.compact_directive
        new_messages, stats = compact_messages(
            self.messages, summarizer=self._pick_summarizer(), directive=directive,
        )
        if stats is None and directive:
            # 轮数不足以走结构化压缩，但用户给了明确要求 → 降级为「指令驱动压缩」
            # （保头 messages[0] + 保尾最近 1 轮 + 中间纯按 directive 压）。
            new_messages, stats = compact_by_directive(
                self.messages, summarizer=self._pick_summarizer(), directive=directive,
            )
        if stats is None:
            # 裸 /compact（无 directive）轮数不足，或历史短到连 1 轮中间都切不出。
            return "轮数不足以压缩（中段为空），本次未压缩。"
        self.messages = new_messages
        note = f"（按要求：{directive}）" if directive else ""
        return (f"已压缩{note}：消息 {stats['before_msgs']}→{stats['after_msgs']} 条，"
                f"压掉 {stats['compacted_turns']} 轮，保留最近 {stats['kept_recent_turns']} 轮，"
                f"摘要 {stats['summary_chars']} 字符。")

    def _maybe_compact(self, usage: dict) -> None:
        """用本轮真实 usage 判断上下文规模，超阈值就压缩 self.messages。

        压缩把中段历史换成一条摘要，self.messages 原地替换成更短的列表；
        下一轮 Think 发出去的就是压缩后的历史。API 无状态，只认我们发的 messages，
        它不知道发生过压缩——控制权全在我们本地。

        T5-A：被动压缩带上会话级偏好 self.compact_directive、按 self.compact_executor 选执行者。
        """
        tokens = current_context_tokens(usage)
        if not should_compact(usage, threshold=self.compact_threshold):
            return  # 没超阈值，不动。

        _log("\n🗜️  [压缩]", f"上下文规模 {tokens} token 超阈值，开始压缩中段历史…")
        new_messages, stats = compact_messages(
            self.messages, summarizer=self._pick_summarizer(),
            directive=self.compact_directive,
        )
        if stats is None:
            # 规模超了但轮数还不够压（切不出中段）——如实说明，不假装压了。
            _log("🗜️  [压缩]", "轮数不足以压缩（中段为空），本轮跳过。")
            return
        self.messages = new_messages
        _log("🗜️  [压缩]",
             f"完成：消息 {stats['before_msgs']}→{stats['after_msgs']} 条，"
             f"压掉 {stats['compacted_turns']} 轮，保留最近 {stats['kept_recent_turns']} 轮，"
             f"摘要 {stats['summary_chars']} 字符。")


# ── P5 Sub-agent 工具：定义在这里（而非 tools.py）以解开循环导入 ──
# 为什么放 agent.py：spawn_subagent 需要 new 一个 Agent。若放 tools.py，就得
# `from agent import Agent`，而 agent.py 顶部又 `from tools import ...` —— 成环。
# 放在 agent.py（Agent 定义之后），它直接用同文件的 Agent，无需任何跨模块 import，
# 逆流边彻底消除，依赖单向朝下（agent → tools，不回头）。
# @tool 装饰器来自 tools（顺流），执行时把本函数注册进 tools.TOOL_SCHEMAS。
# 注意：因此「TOOL_SCHEMAS 里有没有 spawn_subagent」取决于 agent.py 有没有被 import
#       —— 干活工具（read/run/write）导 tools 就注册；这个「关于 Agent 的工具」导 agent 才注册。
#       语义上正确：不 import agent，就没有「派生 agent」的能力。
@tool({"task": "交给子 agent 独立完成的子任务描述，要自包含（说清目标、涉及哪些文件/命令）"})
def spawn_subagent(task: str) -> str:
    """派生一个独立的子 agent 去完成一个子任务，只返回它的最终结论（上下文隔离）。

    什么时候用：当一个子任务需要读很多文件/多步探索，但你只关心它的**结论**时。
    子 agent 自己吭哧跑完（它有独立的对话历史，中间过程不会污染你的上下文），
    只把最终答案回传给你。就像把活外包给助理，你只收一页纸的结果。
    """
    # 子 agent 用**受限工具集**（剔除 spawn_subagent，防无限递归派生），
    # 且给更小的 max_iterations —— 子任务不该像主任务跑那么多轮，防子 agent 自己失控。
    sub = Agent(
        tools=subagent_tool_schemas(),
        max_iterations=15,
    )
    result = sub.run(task)
    # 只回传最终结论。子 agent 的完整历史（sub.messages）留在它自己那里，不回灌主 agent。
    return f"[子 agent 完成] {result}"
