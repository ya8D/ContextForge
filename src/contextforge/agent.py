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

import copy
import inspect
import itertools
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
from contextforge.tools import (
    LocalTool,
    ToolOutput,
    bind_tool_schemas,
    subagent_tool_schemas,
    tool,
    tool_error,
    tool_schemas_for,
    tool_success,
)

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


# 单轮输出 token 上限的默认值（P2）。注意：这是**单轮输出**上限，与 1M **输入**上下文窗口无关。
# 原先硬编码 2048 对编码 agent 过小——连一个中等文件都写不完（write_file 的 content 就发不完整）。
# 8192 是够写完绝大多数单文件、又不铺张的折中；Opus 4.8 单次输出硬上限约 32K，8192 留足余量。
# 对照 Claude Code：其辅助调用用 4096，主循环有「不够时升级 max_tokens」的机制；本项目不做动态
# 升级那套（过重），取一个够用的静态默认 + 可配置即可。
MAX_TOKENS_DEFAULT = 8192
# Anthropic Python SDK 对预计超过 10 分钟的同步请求强制使用 stream；当前阈值约 21,333。
_STREAMING_TOKEN_THRESHOLD = 21_333


def _resolve_max_tokens(explicit: int | None) -> int:
    """解析单轮输出 token 上限。优先级：显式传参 > CONTEXTFORGE_MAX_TOKENS > 默认 8192。

    与 _resolve_compact_threshold 同款兜底：环境变量非法（非正整数）时回默认，不报错——
    配置写错不该让 agent 起不来。写大文件、需要更长单轮输出时可调高（上限受模型约束，约 32K）。
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get("CONTEXTFORGE_MAX_TOKENS")
    if raw:
        try:
            val = int(raw.strip())
            if val > 0:
                return val
        except ValueError:
            pass  # 非法值兜底回默认
    return MAX_TOKENS_DEFAULT


def _to_serializable(obj):
    """把 messages 里的内容转成可 JSON 序列化的形式。

    messages 里混着两种东西：我们自己写的普通 dict，和 SDK 返回的
    content block 对象（如 TextBlock / ToolUseBlock）。后者不是 dict，
    但都有 .model_dump() 能转成 dict。这里递归处理，让整份 messages 能落盘。
    """
    if isinstance(obj, (list, tuple, set)):
        return [_to_serializable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_serializable(v) for k, v in obj.items()}
    if hasattr(obj, "model_dump"):  # anthropic SDK 的 Pydantic 对象
        return _to_serializable(obj.model_dump())
    if isinstance(obj, Path):
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    # trace_metadata 是调用方提供的观测信息，不能让一个不可 JSON 化的值推翻已完成的模型调用。
    return str(obj)


@dataclass
class AgentRunResult:
    """一次 Agent.run() 的结构化运行结果；普通 ``run() -> str`` 保持兼容。

    多 Agent 编排不能靠解析 ``[未完成]`` 这类自然语言判断成功失败，也需要汇总每个
    参与者的 token 和 trace。故额外提供这份控制面结果，而不改变原有调用者拿字符串的契约。
    对照 agent_learning 第 16.4 节 Supervisor：控制面必须读取可判定的参与者状态。
    """

    status: str
    output: str
    usage: dict[str, int]
    trace_ref: str | None
    duration_seconds: float
    stop_reason: str | None
    tool_calls: list[dict]
    error: str | None
    trace_metadata: dict


def _zero_usage() -> dict[str, int]:
    """创建一份新的四字段 usage 计数器。"""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _usage_dict(usage) -> dict[str, int]:
    """把 SDK usage 归一化成控制面固定四字段。"""
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def _merge_usage(target: dict[str, int], addition: dict[str, int]) -> None:
    """把一次主调用、压缩调用或子 Agent 调用计入同一运行总账。"""
    for key in target:
        target[key] += (addition or {}).get(key) or 0


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

    # 哨兵：区分「check_command 没传」（走环境变量兜底）和「显式传 None」（明确不要验证门）。
    # 用 `or` 链做兜底时，`None or env` 会落到 env，无法表达「显式关掉」——子 agent 正需要显式
    # 关掉验证门（不继承主任务/环境的 check_command）。故用一个独一无二的哨兵对象区分两者。
    _UNSET = object()

    def __init__(self, model: str | None = None, max_iterations: int = 100,
                 compact_threshold: int | None = None,
                 check_command=_UNSET,
                 tools: list | None = None,
                 compact_directive: str | None = None,
                 compact_executor: str | None = None,
                 max_tokens: int | None = None,
                 system_prompt: str | None = None,
                 local_tools: list[LocalTool] | None = None,
                 trace_metadata: dict | None = None):
        # 模型 ID 从环境读，不在源码保留任何兜底 ID（见 CLAUDE.md）。显式参数只用于
        # 测试/依赖注入；正常 CLI 必须配置 ANTHROPIC_MODEL，漏配时在启动处立即暴露。
        self.model = model or os.environ.get("ANTHROPIC_MODEL")
        if not self.model:
            raise RuntimeError("未配置 ANTHROPIC_MODEL，无法创建 Agent")
        # SDK 自动读取 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL。
        self.client = anthropic.Anthropic()
        # 硬护栏：循环最多转几轮，防止失控无限循环烧 token（第 1.3 节 护栏）。
        self.max_iterations = max_iterations
        # 单轮输出 token 上限（P2）。优先级：显式传参 > 环境变量 CONTEXTFORGE_MAX_TOKENS > 默认 8192。
        # 原先硬编码 2048 过小、大文件一轮写不完；做成可配置，写大文件时可调高（≤ 模型约 32K 上限）。
        self.max_tokens = _resolve_max_tokens(max_tokens)
        # 上下文压缩阈值（真实 token）。优先级：显式传参 > 环境变量 CONTEXTFORGE_COMPACT_THRESHOLD
        # （写 .env 持久生效，Chromium 这类大项目可调高以用满更多上下文再压）> 默认 500K。
        # 测试可传小值以便低成本触发压缩、验证「压缩链真的通」而不必真塞 500K。
        self.compact_threshold = _resolve_compact_threshold(compact_threshold)
        # schema / handler / 并发属性在 tools.py 的同一注册锁内原子绑定。所有 Agent 都冻结构造时
        # 能力；运行中热注册不能偷换在途请求的说明书或实现。
        base_tool_schemas, handlers, concurrency, global_names = bind_tool_schemas(tools)

        # 多 Agent 协作的 submit_* 工具捕获本次运行状态，不写进全局注册表。实例工具名不得
        # 冒用任何全局工具——否则 Harness 会按全局语义检查参数，执行层却跑另一份 handler。
        local_tool_list = list(local_tools or [])
        local_names = [local.name for local in local_tool_list]
        if len(local_names) != len(set(local_names)):
            raise ValueError("实例级工具名不能重复")
        duplicate_names = global_names & set(local_names)
        if duplicate_names:
            raise ValueError(f"实例级工具与全局工具重名：{', '.join(sorted(duplicate_names))}")
        self.local_tools = {local.name: local for local in local_tool_list}
        self.tool_schemas = [
            *base_tool_schemas,
            *(local.schema for local in local_tool_list),
        ]
        self._global_tool_handlers = handlers
        self._global_tool_concurrency = concurrency
        self.system_prompt = system_prompt
        self.trace_metadata = copy.deepcopy(trace_metadata or {})
        # Agent 是有状态会话对象，不支持同实例并发/递归 run。团队并行必须每个 Worker 独占实例；
        # 锁只做“拒绝重入”，不把两个任务排队到同一份 messages 上悄悄串线。
        self._run_lock = threading.Lock()
        # 每次 run() 都会清零；逐轮累加供 run_detailed / team usage 分账读取。
        self._last_run_usage = _zero_usage()
        self._last_run_tool_calls: list[dict] = []
        self._last_stop_reason: str | None = None
        self._last_run_status = "succeeded"
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
        # 优先级：显式传参（含显式 None=明确不要）> 环境变量 CONTEXTFORGE_CHECK_COMMAND > None（跳过）。
        # 用哨兵区分「没传」和「显式 None」：没传才读环境变量兜底；显式 None 直接关掉验证门
        # （子 agent 就靠这个不继承主任务/环境的 check_command）。存一份到实例供 CLI /check 查看/复用。
        if check_command is Agent._UNSET:
            self.check_command = os.environ.get("CONTEXTFORGE_CHECK_COMMAND") or None
        else:
            self.check_command = check_command or None
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
        """跑一个任务，返回模型的最终文字答案（旧接口保持不变）。"""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("同一个 Agent 实例不能并发或递归运行")
        try:
            return self._run_once(task)
        finally:
            self._run_lock.release()

    def _run_once(self, task: str) -> str:
        """持锁执行一次任务，并让失败任务的消息与权限状态一起回滚。"""
        # 先清零本次状态，再做 trace mkdir 等可能失败的 I/O；早期异常也不能带出上次统计。
        self._last_run_usage = _zero_usage()
        self._last_run_tool_calls = []
        self._last_stop_reason = None
        self._last_run_status = "succeeded"

        self.task_counter += 1
        self.current_task_dir = self.trace_dir / f"task_{self.task_counter:02d}"
        if _trace_enabled():
            self.current_task_dir.mkdir(parents=True, exist_ok=True)

        self.loop_detector.reset()
        _log("\n🎯 [任务]", task)
        if _trace_enabled():
            _log("📁 [trace]", f"本任务 in/out 落盘到 {self.current_task_dir}")

        # messages 代表模型知道什么，read_files 代表模型基于这些内容获得的写权限；二者必须作为
        # 同一事务回滚。否则任务读文件后 API 失败，历史已忘记正文，下一任务却仍能直接覆盖文件。
        _msgs_snapshot = list(self.messages)
        _read_files_snapshot = set(self.read_files)
        self.messages.append({"role": "user", "content": task})

        try:
            result = self._run_loop()
            if self._last_run_status == "failed":
                self.messages[:] = _msgs_snapshot
                self.read_files.clear()
                self.read_files.update(_read_files_snapshot)
            return result
        except BaseException:
            self._last_run_status = "failed"
            self.messages[:] = _msgs_snapshot
            self.read_files.clear()
            self.read_files.update(_read_files_snapshot)
            raise

    def run_detailed(self, task: str) -> AgentRunResult:
        """执行一次 TAOR，并原子快照控制面结果（对照 agent_learning 第 16.4 节）。"""
        started = time.perf_counter()
        if not self._run_lock.acquire(blocking=False):
            return AgentRunResult(
                status="failed",
                output="",
                usage=_zero_usage(),
                trace_ref=None,
                duration_seconds=time.perf_counter() - started,
                stop_reason=None,
                tool_calls=[],
                error="RuntimeError: 同一个 Agent 实例不能并发或递归运行",
                trace_metadata=copy.deepcopy(self.trace_metadata),
            )

        error = None
        try:
            try:
                output = self._run_once(task)
                status = self._last_run_status
            except Exception as exc:  # noqa: BLE001 —— 结构化记录给协调器
                status = "failed"
                output = ""
                error = f"{type(exc).__name__}: {exc}"
            duration = time.perf_counter() - started
            trace_ref = (
                str(getattr(self, "current_task_dir", self.trace_dir))
                if _trace_enabled() else None
            )
            if status != "succeeded" and error is None:
                error = output or "Agent 未完成任务"
            return AgentRunResult(
                status=status,
                output=output,
                usage=dict(self._last_run_usage),
                trace_ref=trace_ref,
                duration_seconds=duration,
                stop_reason=self._last_stop_reason,
                tool_calls=copy.deepcopy(self._last_run_tool_calls),
                error=error,
                trace_metadata=copy.deepcopy(self.trace_metadata),
            )
        finally:
            self._run_lock.release()

    def tool_calls_snapshot(self) -> list[dict]:
        """返回本次运行工具记录的独立快照，供证据门按路径与 TAOR 轮次核验。"""
        return copy.deepcopy(self._last_run_tool_calls)

    def has_executed_tool(self, name: str, *, succeeded: bool = False) -> bool:
        """本次 run 中某工具是否真正执行过；可要求执行结果成功。"""
        return any(
            call["name"] == name
            and call.get("executed", False)
            and (not succeeded or call.get("succeeded", False))
            for call in self._last_run_tool_calls
        )

    def _execute_tool(self, name: str, tool_input: dict) -> ToolOutput:
        """分发实例级或绑定后的全局工具，并统一机器可读结果语义。"""
        local = self.local_tools.get(name)
        if local is not None:
            try:
                result = local.handler(copy.deepcopy(tool_input))
            except Exception as exc:  # noqa: BLE001 —— 错误回喂让模型纠正
                return tool_error(f"[错误] 实例工具 {name} 执行异常：{exc}")
            if isinstance(result, ToolOutput):
                return result
            if isinstance(result, str):
                # 普通字符串只代表成功；失败必须显式返回 tool_error，不能再猜正文前缀。
                return tool_success(result)
            return tool_error(
                f"[错误] 实例工具 {name} 返回类型不合法：{type(result).__name__}"
            )

        # 全局工具也使用构造时原子绑定的 handler，确保执行实现与发给模型的 schema 同版本。
        func = self._global_tool_handlers.get(name)
        if func is None:
            return tool_error(f"[错误] 未知工具：{name}")
        signature = inspect.signature(func)
        call_kwargs = {
            key: copy.deepcopy(value)
            for key, value in tool_input.items()
            if not str(key).startswith("_")
        }
        injected = {
            "_read_files": self.read_files,
            "_model": self.model,
            "_max_tokens": self.max_tokens,
            "_parent_trace": str(getattr(self, "current_task_dir", self.trace_dir)),
        }
        for parameter, value in injected.items():
            if parameter in signature.parameters:
                call_kwargs[parameter] = value
        try:
            signature.bind(**call_kwargs)
        except TypeError as exc:
            return tool_error(f"[错误] 工具 {name} 参数不对：{exc}")
        try:
            result = func(**call_kwargs)
        except Exception as exc:  # noqa: BLE001
            return tool_error(f"[错误] 工具 {name} 执行异常：{exc}")
        if isinstance(result, ToolOutput):
            return result
        if isinstance(result, str):
            # 普通字符串只代表成功；失败必须显式返回 tool_error，避免合法正文前缀误判。
            return tool_success(result)
        return tool_error(f"[错误] 工具 {name} 返回类型不合法：{type(result).__name__}")

    def _is_concurrency_safe(self, name: str) -> bool:
        """实例工具优先；全局工具使用构造时绑定的并发属性快照。"""
        local = self.local_tools.get(name)
        if local is not None:
            return local.concurrency_safe
        return self._global_tool_concurrency.get(name, False)

    def _record_unexecuted_tool_uses(self, tool_use_blocks, turn: int, reason: str) -> None:
        """审计因停止原因而未进入 Act 的工具请求，避免控制面静默漏记。"""
        allowed_names = {schema["name"] for schema in self.tool_schemas}
        for block in tool_use_blocks:
            self._last_run_tool_calls.append({
                "tool_use_id": block.id,
                "name": block.name,
                "input": _to_serializable(block.input),
                "turn": turn,
                "sequence": len(self._last_run_tool_calls),
                "allowed": block.name in allowed_names,
                "executed": False,
                "succeeded": False,
                "reason": reason,
            })

    def _pair_pending_tool_uses(self, tool_use_blocks, note: str) -> dict:
        """给一组「未执行」的 tool_use 生成一条配对的 user(tool_result) 消息（审查 #1）。

        Anthropic API 硬性要求：assistant 消息里每个 tool_use block，必须在紧邻的下一条 user
        消息里有 tool_use_id 配对的 tool_result，否则下一轮请求直接被拒（真实复现：本地代理
        以 InternalServerError(500) 包上游 400）。当 harness 决定**不执行**本轮工具（死循环打断）
        时，这些 tool_use 已经进了历史，必须补一条占位 tool_result 把配对补齐，才能安全继续。
        note 说明为何未执行。（日后若接原生端点、需处理 max_tokens 半截 tool_use，也可复用本辅助。）
        """
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": note,
                    "is_error": True,
                }
                for b in tool_use_blocks
            ],
        }

    def _run_loop(self) -> str:
        """TAOR 主循环本体（从 run() 抽出，便于 run() 在外层统一做失败回滚）。"""
        for i in range(1, self.max_iterations + 1):
            _log(f"\n🔄 ===== TAOR 第 {i}/{self.max_iterations} 轮 =====", "")

            # trace 关闭时不复制完整历史；多轮大文件会话里这能避免无收益的近 O(n²) 序列化。
            messages_sent = _to_serializable(self.messages) if _trace_enabled() else None

            # ── Think：调 LLM，让模型决定下一步 ──
            request = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": self.messages,
            }
            # 有工具才传 tools；最终 Coordinator 汇总阶段是纯文本调用，空列表直接省略更符合 API 语义。
            if self.tool_schemas:
                request["tools"] = self.tool_schemas
            # Anthropic 的 system 是顶层参数，不塞进 messages；None 时不传，保证普通 Agent 请求
            # 形态与旧实现一致。Coordinator/Worker/Reviewer 用它形成真正的角色分工。
            if self.system_prompt:
                request["system"] = self.system_prompt
            if self.max_tokens > _STREAMING_TOKEN_THRESHOLD:
                with self.client.messages.stream(**request) as stream:
                    response = stream.get_final_message()
            else:
                response = self.client.messages.create(**request)

            usage = _usage_dict(response.usage)
            self._last_stop_reason = response.stop_reason
            _merge_usage(self._last_run_usage, usage)

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

            # ── 判断：只有 end_turn 才是完整自然结束 ──
            final = "".join(b.text for b in response.content if b.type == "text")
            if response.stop_reason == "refusal":
                # 拒绝不是可复用会话历史：移除刚追加的 assistant refusal，保留用户任务，调用方可
                # 换模型/提示后继续；run_detailed 仍如实标 failed。
                self.messages.pop()
                self._last_run_status = "failed"
                return final or "[拒绝] 模型拒绝完成本次任务。"
            if response.stop_reason == "pause_turn":
                # pause_turn 正常只包含服务端工具状态；若同时出现客户端 tool_use，必须先本地执行并
                # 配对，不能直接 assistant→assistant 重放。保守标 incomplete，让调用方重试。
                pending = [block for block in response.content if block.type == "tool_use"]
                if pending:
                    reason = "pause_turn 响应含客户端 tool_use，等待显式重试"
                    self._record_unexecuted_tool_uses(pending, i, reason)
                    self.messages.append(self._pair_pending_tool_uses(
                        pending,
                        f"[未执行] {reason}。",
                    ))
                    self._last_run_status = "incomplete"
                    return "[未完成] pause_turn 含客户端工具调用。"
                # 纯服务端工具状态已在上面追加，下一次 Think 原样续传。
                continue
            if response.stop_reason in {"max_tokens", "stop_sequence"}:
                # 原生端点可能在 max_tokens 时留下 tool_use block。它已经进入历史，先补错误结果
                # 保证下一任务重放历史时仍满足 tool_use/tool_result 配对，再如实标记 incomplete。
                pending = [block for block in response.content if block.type == "tool_use"]
                if pending:
                    reason = f"模型因 {response.stop_reason} 停止，工具参数可能不完整"
                    self._record_unexecuted_tool_uses(pending, i, reason)
                    self.messages.append(self._pair_pending_tool_uses(
                        pending,
                        f"[未执行] {reason}。",
                    ))
                self._last_run_status = "incomplete"
                partial = final.strip()
                return (
                    f"[未完成] stop_reason={response.stop_reason}"
                    + (f"\n{partial}" if partial else "")
                )
            if response.stop_reason == "end_turn":
                if not final.strip():
                    self._last_run_status = "incomplete"
                    return "[未完成] 模型自然结束但没有文本答案。"
                passed, report = self.validation_gate.verify(self._run_check)
                if passed:
                    # 压缩是可观测性/成本优化，不能反过来推翻已经完成且通过验证的业务答案。
                    # summarizer 异常时保留原历史并交付 final，下个任务仍可继续。
                    try:
                        self._maybe_compact(usage)
                    except Exception as exc:  # noqa: BLE001
                        _log("🗜️ [压缩失败]", f"保留原历史：{type(exc).__name__}: {exc}", level="error")
                    _log("\n✅ [完成]", "模型自然结束，且通过验证门，循环结束。")
                    return final
                _log("\n🚧 [验证门]", "未通过，打回让模型继续修复。", level="error")
                self.messages.append({
                    "role": "user",
                    "content": (f"[验证门] 你声称完成了，但强制检查未通过。"
                                f"不要删除或修改检查/测试来蒙混，请修复真正的问题后继续。\n{report}"),
                })
                continue
            if response.stop_reason != "tool_use":
                self._last_run_status = "incomplete"
                return final or f"[未完成] 未识别的 stop_reason={response.stop_reason}"

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
                note = "[被 harness 中断] 检测到死循环，本次工具调用未执行。"
                for block in tool_use_blocks:
                    self._last_run_tool_calls.append({
                        "tool_use_id": block.id,
                        "name": block.name,
                        "input": _to_serializable(block.input),
                        "turn": i,
                        "sequence": len(self._last_run_tool_calls),
                        "allowed": False,
                        "executed": False,
                        "succeeded": False,
                        "reason": "检测到死循环",
                    })
                self.messages.append(self._pair_pending_tool_uses(tool_use_blocks, note))
                self.messages.append({
                    "role": "user",
                    "content": ("[死循环检测] 你连续多次重复了完全相同的操作但没有进展。"
                                "请停下来换一个思路：换个命令/参数、或换个角度分析问题，"
                                "不要再重复刚才那个动作。"),
                })
                continue

            # ① 权限关卡（对照第 8.2 支柱二）：执行前逐个过 check_tool_call。
            # 命中危险（rm -rf / 路径遍历等）→ 不执行，把拒绝原因当结果回喂让模型换做法。
            # 用代码强制，不靠模型自律 —— 这是本项目的 harness 核心。
            # 先算好每个工具「放行/拒绝」，放行的才真正并行执行。
            gate_results: list[tuple] = []  # (block, 是否放行, 拒绝原因或 None)
            to_execute = []                  # 放行的 block（送去并行执行）
            # 本轮实际发给模型的菜单就是唯一授权源；不要再维护可漂移的名称快照。
            current_allowed = {schema["name"] for schema in self.tool_schemas}
            for block in tool_use_blocks:
                if block.name not in current_allowed:
                    # 工具菜单不是安全边界：模型即使幻觉出没展示的工具名，也必须在执行层硬拒绝。
                    ok, reason = False, f"工具 {block.name} 不在本 Agent 的授权工具白名单中（未授权）"
                else:
                    try:
                        ok, reason = check_tool_call(block.name, block.input)
                    except (AttributeError, TypeError, ValueError) as exc:
                        ok, reason = False, f"工具参数不合法：{exc}"
                call_record = {
                    "tool_use_id": block.id,
                    "name": block.name,
                    "input": _to_serializable(block.input),
                    "turn": i,
                    "sequence": len(self._last_run_tool_calls),
                    "allowed": ok,
                    "executed": False,
                    "succeeded": False,
                    "reason": None if ok else reason,
                }
                self._last_run_tool_calls.append(call_record)
                gate_results.append((block, ok, None if ok else reason, call_record))
                if ok:
                    to_execute.append(block)
                else:
                    _log("🛡️ [权限]", f"拦截 {block.name} 参数={block.input} —— {reason}", level="error")

            # 执行放行的工具（P1：按并发安全分批，**保持模型给出的原始顺序**）。
            # 对照 Claude Code toolOrchestration：只把**相邻的连续只读工具**分为一组并发跑；
            # 一遇到有副作用工具（write_file/run_command/spawn_subagent）就断开、串行执行它，再继续。
            # 为什么必须保序（P1 part2 修正，真实 API 复现）：早先「所有 read 挑出来先并发、所有 write
            # 挑出来后串行」的做法打破了原始顺序——模型若在同一轮请求 [write(X,新), read(X)]（先写后
            # 读回确认），read 会被提前跑、读到**旧内容**。按原始顺序分组则 write 先串行、read 后跑，
            # 读到新内容，与模型意图一致；同时相邻只读段仍并发，不丢并发收益。
            # SDK 约定同一响应的 tool_use_id 唯一；本地先校验，避免重复 ID 导致结果覆盖/错配。
            ids = [block.id for block in tool_use_blocks]
            if len(ids) != len(set(ids)):
                self.messages.append(self._pair_pending_tool_uses(
                    tool_use_blocks,
                    "[未执行] 同一响应出现重复 tool_use_id，无法安全配对结果。",
                ))
                self._last_run_status = "failed"
                return "[错误] 模型返回重复 tool_use_id。"
            executed: dict = {}  # block.id -> 执行结果
            i = 0
            while i < len(to_execute):
                if self._is_concurrency_safe(to_execute[i].name):
                    # 收集从 i 起**连续的**只读工具，作为一个并发组。
                    j = i
                    while j < len(to_execute) and self._is_concurrency_safe(to_execute[j].name):
                        j += 1
                    group = to_execute[i:j]
                    if len(group) == 1:
                        b = group[0]
                        executed[b.id] = self._execute_tool(b.name, b.input)
                    else:
                        # read_files 带外注入本实例的「已读集合」——read_file/write_file 收到它，别的
                        # 工具忽略；每个 Agent 各写各的 set，并发不竞态、子 agent 不泄漏。
                        with ThreadPoolExecutor(max_workers=8) as pool:
                            results = list(pool.map(
                                lambda b: self._execute_tool(b.name, b.input),
                                group,
                            ))
                        for b, r in zip(group, results):
                            executed[b.id] = r
                    i = j
                else:
                    # 有副作用工具：串行、就地执行，绝不与前后并发（消除同轮写竞态 + 保序）。
                    b = to_execute[i]
                    executed[b.id] = self._execute_tool(b.name, b.input)
                    i += 1

            # 组装 Observe：机器状态直接来自 ToolOutput.is_error，并同步进 Anthropic is_error。
            tool_results = []
            terminal_succeeded = False
            any_tool_error = False
            for block, ok, reason, call_record in gate_results:
                if ok:
                    result = executed[block.id]
                    call_record["executed"] = True
                    call_record["succeeded"] = not result.is_error
                    raw_result = str(result)
                    call_record["result_preview"] = raw_result[:300]
                    if block.name == "read_file" and not result.is_error:
                        # evidence 只能引用模型实际看到的读取范围；超长结果在 50K 处截断，不能把
                        # 截断后未回喂的行也算作“已读”。末尾半行仍可见，splitlines 会计入。
                        call_record["line_count"] = len(
                            raw_result[:_MAX_RESULT_CHARS].splitlines()
                        )
                    if result.trace_ref:
                        call_record["trace_ref"] = result.trace_ref
                    _merge_usage(self._last_run_usage, result.usage)
                    _log("🦾 [Act]", f"调用工具 {block.name}  参数={block.input}")
                    preview = str(result) if len(result) <= 300 else str(result)[:300] + " …(打印截断)"
                    _log("👀 [Observe]", preview)
                    content = _truncate_for_feedback(str(result))
                    is_error = result.is_error
                    any_tool_error = any_tool_error or is_error
                    local = self.local_tools.get(block.name)
                    terminal_succeeded = terminal_succeeded or bool(
                        local and local.terminal and not result.is_error
                    )
                else:
                    content = f"[被 harness 拒绝] {reason}。请改用更安全的做法。"
                    is_error = True
                    any_tool_error = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                })

            # 先保存配对 tool_result，再让成功终态工具结束；trace/history 始终保持协议完整。
            self.messages.append({"role": "user", "content": tool_results})
            if terminal_succeeded and not any_tool_error:
                passed, report = self.validation_gate.verify(self._run_check)
                if passed:
                    _log("\n✅ [完成]", "终态结构化提交已接收，且通过验证门。")
                    return "结构化结果已提交。"
                _log("\n🚧 [验证门]", "终态提交后检查未通过，标记未完成。", level="error")
                self._last_run_status = "incomplete"
                return f"[未完成] 终态提交后的验证门未通过。\n{report}"

            self._maybe_compact(usage)

        # 达到最大轮数还没结束 —— 护栏触发。结构化接口必须把它标成 incomplete，
        # 不能再让编排层看到一个非空字符串就误以为成功。
        self._last_run_status = "incomplete"
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
            "max_tokens": self.max_tokens,
            "system": self.system_prompt,
            "tools": _to_serializable(self.tool_schemas),
            "stop_reason": stop_reason,
            "usage": usage,
            "trace_metadata": _to_serializable(self.trace_metadata),
            "messages_sent": messages_sent,
            "response_content": _to_serializable(response_content),
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
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.max_tokens > _STREAMING_TOKEN_THRESHOLD:
            with self.client.messages.stream(**request) as stream:
                resp = stream.get_final_message()
        else:
            resp = self.client.messages.create(**request)
        _merge_usage(self._last_run_usage, _usage_dict(resp.usage))
        if resp.stop_reason != "end_turn":
            raise RuntimeError(f"压缩摘要未完整结束：stop_reason={resp.stop_reason}")
        summary = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not summary:
            raise RuntimeError("压缩摘要为空")
        return summary

    def _run_check(self, command: str, timeout: int = 300) -> tuple[int | None, str]:
        """验证门的 runner 回调：跑检查命令（如 pytest）拿 (退出码, 输出)。

        复用 tools.run_command_with_exit——同 summarizer 注入思路：ValidationGate 只懂
        「验证的判据」，具体怎么跑命令由这个注入的回调完成，逻辑与副作用分离。
        审查 #3/#4：返回退出码（成败唯一可靠判据）+ 可配 timeout（慢测试套件不被 30s 杀）。
        """
        from contextforge.tools import run_command_with_exit
        return run_command_with_exit(command, timeout=timeout)

    def _summarize_via_subagent(self, prompt: str) -> str:
        """T5-A 执行者「subagent」：派一个带工具的子 agent 去做压缩摘要。

        与 _summarize（盲总结一次）的区别：子 agent 有独立上下文 + 受限工具集
        （read/run/write），能**回读原始文件核实**某个结论是否还成立，而非凭记忆总结。
        这就是「压缩执行者可切换成子 agent」的增量价值（复用 P5 sub-agent 机制）。
        子 agent 的中间过程留在它自己的 messages 里，只回传最终摘要。
        """
        sub = Agent(
            model=self.model,
            # 压缩只需回读/检索核实，绝不需要 write_file；能力边界在派生点正向声明。
            tools=tool_schemas_for({"read_file", "run_command"}),
            max_iterations=15,
            check_command=None,
            # 防压缩子 Agent 再按环境变量派生压缩子子 Agent；一层派生是代码硬边界。
            compact_executor="self",
            trace_metadata={
                **self.trace_metadata,
                "role": "compact_subagent",
                "parent_trace": str(getattr(self, "current_task_dir", self.trace_dir)),
            },
        )
        task = (
            "你是一个上下文压缩助手。下面给你一段 agent 的中间对话历史和压缩要求。"
            "请产出符合要求的『前情摘要』。如果历史里提到某个文件/命令的结论，你可以用"
            "read_file / run_command **回读核实**它现在是否还成立，再据实写进摘要。"
            "只输出摘要正文。\n\n" + prompt
        )
        detail = sub.run_detailed(task)
        _merge_usage(self._last_run_usage, detail.usage)
        if detail.status != "succeeded" or not detail.output.strip():
            raise RuntimeError(detail.error or "压缩子 Agent 未完整产出摘要")
        return detail.output

    def _pick_summarizer(self):
        """按 self.compact_executor 选压缩执行者回调（self=盲总结 / subagent=带工具核实）。"""
        return self._summarize_via_subagent if self.compact_executor == "subagent" else self._summarize

    def compact_now(self, directive: str | None = None) -> str:
        """主动压缩入口；与 TAOR 共用运行锁，不能并发改写会话历史。"""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Agent 正在运行，不能同时压缩历史")
        try:
            directive = directive or self.compact_directive
            summarizer = self._pick_summarizer()
            new_messages, stats = compact_messages(
                self.messages, summarizer=summarizer, directive=directive,
            )
            if stats is None and directive:
                new_messages, stats = compact_by_directive(
                    self.messages, summarizer=summarizer, directive=directive,
                )
            if stats is None:
                return "轮数不足以压缩（中段为空），本次未压缩。"
            self.messages = new_messages
            note = f"（按要求：{directive}）" if directive else ""
            return (
                f"已压缩{note}：消息 {stats['before_msgs']}→{stats['after_msgs']} 条，"
                f"压掉 {stats['compacted_turns']} 轮，保留最近 {stats['kept_recent_turns']} 轮，"
                f"摘要 {stats['summary_chars']} 字符。"
            )
        finally:
            self._run_lock.release()

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
def spawn_subagent(
    task: str,
    _model: str | None = None,
    _max_tokens: int | None = None,
    _parent_trace: str | None = None,
) -> ToolOutput:
    """派生一个独立的子 agent 去完成一个子任务，只返回它的最终结论（上下文隔离）。

    什么时候用：当一个子任务需要读很多文件/多步探索，但你只关心它的**结论**时。
    子 agent 自己吭哧跑完（它有独立的对话历史，中间过程不会污染你的上下文），
    只把最终答案回传给你。就像把活外包给助理，你只收一页纸的结果。
    """
    # 子 agent 用**受限工具集**（剔除 spawn_subagent，防无限递归派生），
    # 且给更小的 max_iterations —— 子任务不该像主任务跑那么多轮，防子 agent 自己失控。
    # check_command=None：验证门是主任务概念，子 agent 不该继承（否则回退读环境变量 CONTEXTFORGE_
    # CHECK_COMMAND，被无关检查命令反复打回、撞 max_iterations 返回垃圾结论）。
    sub = Agent(
        model=_model,
        tools=subagent_tool_schemas(),
        max_iterations=15,
        max_tokens=_max_tokens,
        check_command=None,
        compact_executor="self",
        trace_metadata={
            "role": "spawned_subagent",
            "parent_trace": _parent_trace,
        },
    )
    detail = sub.run_detailed(task)
    content = detail.output or detail.error or "子 Agent 未返回结果"
    trace_ref = detail.trace_ref
    if detail.status == "succeeded":
        return tool_success(
            f"[子 agent 完成] {content}", usage=detail.usage, trace_ref=trace_ref
        )
    return tool_error(
        f"[子 agent {detail.status}] {content}", usage=detail.usage, trace_ref=trace_ref
    )
