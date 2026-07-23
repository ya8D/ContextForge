"""
tools.py —— 工具层（对照 agent_learning 第 3 章 Tool Use / Function Calling）

核心认知（第 3.2 节）：
- 工具函数 = 真正干活的「手」（读文件、跑命令、写文件）。
- 工具 schema = 写给模型看的「说明书」，告诉它有什么工具、怎么调。
- 模型只**决定**调哪个工具、传什么参数；**执行**由这里的 Python 代码完成。

Phase 2 升级（对照第 3.3 节）：
- 用 @tool 装饰器从**函数签名 + docstring** 自动生成 input_schema，
  告别手写 TOOL_SCHEMAS。加新工具只写函数，schema 自动来。
- 加 write_file 工具，并配合 agent 层实现「先读再改」硬约束。
"""

import copy
import inspect
import locale
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, get_type_hints

# ─────────────────────────────────────────────────────────────
# 0. @tool 装饰器 + 自动注册表
# ─────────────────────────────────────────────────────────────
# 两张表在 import 时由装饰器自动填充：
#   TOOL_FUNCTIONS：工具名 → 真正的 Python 函数（供循环层按名分发）
#   TOOL_SCHEMAS  ：给模型看的说明书列表（Anthropic 的 name/description/input_schema）
TOOL_FUNCTIONS: dict = {}
TOOL_SCHEMAS: list = []
# 工具名 → 是否「并发安全」（P1）：只读工具并发安全（read_file），有副作用的不安全
# （write_file/run_command 会改磁盘/跑命令，同轮并行写同一文件是竞态）。主循环据此把一轮里
# 的工具分成「只读并发批」+「有副作用串行批」，对照 Claude Code toolOrchestration 的 isConcurrencySafe。
TOOL_CONCURRENCY_SAFE: dict = {}
# schema / handler / 并发属性必须作为同一版本发布和读取，避免热注册期间观察到混合状态。
_TOOL_REGISTRY_LOCK = threading.RLock()

# Python 类型注解 → JSON Schema 类型的简单映射。
_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_INJECTED_CONTEXT_PARAMS = {
    "_read_files",
    "_timeout",
    "_model",
    "_max_tokens",
    "_parent_trace",
}


class ToolOutput(str):
    """兼容字符串的结构化工具结果，额外携带机器可读错误状态。

    真实文件正文可能恰好以 ``[错误]`` 开头，不能再让 Agent 猜前缀。字符串子类保持
    现有打印、截断和测试兼容，同时让控制面和 Anthropic ``tool_result.is_error`` 读取
    同一个状态。对照 agent_learning 第 3.2 节：工具错误也是显式的 Observe 结果。
    """

    def __new__(
        cls,
        content: str,
        *,
        is_error: bool = False,
        usage: dict[str, int] | None = None,
        trace_ref: str | None = None,
    ):
        if not isinstance(content, str):
            raise TypeError("ToolOutput.content 必须是字符串")
        if not isinstance(is_error, bool):
            raise TypeError("ToolOutput.is_error 必须是 bool")
        normalized_usage = {}
        for key, raw in (usage or {}).items():
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise ValueError(f"ToolOutput.usage[{key}] 必须是非负整数")
            normalized_usage[key] = raw
        value = super().__new__(cls, content)
        value.is_error = is_error
        value.usage = normalized_usage
        value.trace_ref = trace_ref
        return value


def tool_success(
    content: str,
    *,
    usage: dict[str, int] | None = None,
    trace_ref: str | None = None,
) -> ToolOutput:
    """创建机器可读的成功工具结果。"""
    return ToolOutput(content, is_error=False, usage=usage, trace_ref=trace_ref)


def tool_error(
    content: str,
    *,
    usage: dict[str, int] | None = None,
    trace_ref: str | None = None,
) -> ToolOutput:
    """创建机器可读的失败工具结果。"""
    return ToolOutput(content, is_error=True, usage=usage, trace_ref=trace_ref)


@dataclass(frozen=True)
class LocalTool:
    """只属于某个 Agent 实例的工具，不写入进程级全局注册表。

    全局 ``@tool`` 适合 read_file 这类所有 Agent 都能复用的基础能力；多 Agent 协作里的
    ``submit_plan`` / ``submit_worker_report`` / ``submit_review`` 则各自捕获本次运行的状态，
    若也注册成全局工具会互相覆盖 handler、污染其它 Agent。故给它们一个实例级定义：显式
    schema 支持嵌套 tasks[]，handler 直接接收模型提交的整份 input dict。
    对照 agent_learning 第 16.2～16.3 节：结构化通信与角色上下文隔离。
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], str]
    concurrency_safe: bool = False
    # submit_plan / submit_worker_report / submit_review 是角色的终态交付；成功后无需再烧一轮
    # API 等模型说“结束”。失败结果不终止，仍让模型根据 is_error 自我修正。
    terminal: bool = False

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError(f"实例级工具名不合法：{self.name}")
        if self.input_schema.get("type") != "object":
            raise ValueError("实例级工具 input_schema 顶层必须是 object")
        if not isinstance(self.concurrency_safe, bool):
            raise TypeError("LocalTool.concurrency_safe 必须是 bool")
        if not isinstance(self.terminal, bool):
            raise TypeError("LocalTool.terminal 必须是 bool")

    @property
    def schema(self) -> dict:
        """转换成 Anthropic Messages API 接受的工具说明书，并隔离可变 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema),
        }


def tool(param_desc: dict | None = None, concurrency_safe: bool = False):
    """把一个普通函数注册成 agent 工具，并自动生成它的 input_schema。

    用法：
        @tool({"path": "文件路径"})
        def read_file(path: str) -> str:
            '''读取文本文件内容。'''  # ← docstring 作为工具描述

    自动做的事：
    - 工具名 = 函数名
    - 工具描述 = 函数的 docstring 第一段
    - 参数 = 函数签名里的每个参数（类型来自注解，说明来自 param_desc）
    - 必填参数 = 签名里没有默认值的参数

    concurrency_safe（P1）：本工具是否可与同轮其它工具**并发**执行。默认 False（保守兜底：
    未显式声明安全的都当不安全、串行执行）。只读工具（read_file）标 True；有副作用的
    （write_file/run_command）保持 False——同轮并行写同一文件是竞态，必须串行。
    """
    param_desc = param_desc or {}

    def decorator(func):
        if not _TOOL_NAME_RE.fullmatch(func.__name__):
            raise ValueError(f"全局工具名不合法：{func.__name__}")
        sig = inspect.signature(func)
        try:
            type_hints = get_type_hints(func)
        except (NameError, TypeError):
            type_hints = {}
        properties = {}
        required = []
        for pname, p in sig.parameters.items():
            if p.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD}:
                raise ValueError(f"工具参数必须可用关键字调用：{func.__name__}.{pname}")
            if pname.startswith("_"):
                if pname not in _INJECTED_CONTEXT_PARAMS:
                    raise ValueError(f"未知带外依赖参数：{func.__name__}.{pname}")
                continue
            annotation = type_hints.get(pname, p.annotation)
            if annotation not in _PY_TO_JSON:
                raise ValueError(f"暂不支持的工具参数类型：{func.__name__}.{pname}={annotation}")
            json_type = _PY_TO_JSON[annotation]
            properties[pname] = {
                "type": json_type,
                "description": param_desc.get(pname, ""),
            }
            if p.default is inspect.Parameter.empty:  # 无默认值 → 必填
                required.append(pname)

        # docstring 第一段作为工具描述（去掉缩进和空行）。
        doc = inspect.getdoc(func) or ""
        description = doc.split("\n\n")[0].strip()

        schema = {
            "name": func.__name__,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }
        # 模块 reload / Notebook 重跑时，同名工具作为一个版本替换；锁保证 schema、handler、
        # 并发属性不会被其它线程观察成混合版本。
        with _TOOL_REGISTRY_LOCK:
            existing = [
                i for i, item in enumerate(TOOL_SCHEMAS)
                if item["name"] == func.__name__
            ]
            if existing:
                first = existing[0]
                TOOL_SCHEMAS[first] = schema
                for index in reversed(existing[1:]):
                    del TOOL_SCHEMAS[index]
            else:
                TOOL_SCHEMAS.append(schema)
            TOOL_FUNCTIONS[func.__name__] = func
            TOOL_CONCURRENCY_SAFE[func.__name__] = concurrency_safe
        return func

    return decorator


# ─────────────────────────────────────────────────────────────
# 1. 工具函数：真正干活的「手」（加 @tool 即自动注册 + 生成 schema）
# ─────────────────────────────────────────────────────────────

# 「本会话读过哪些文件」的状态供 write_file 的「先读再改」约束检查。
# ⚠️ 这个状态是 **Agent 实例状态**（agent.py 的 self.read_files），由 execute_tool 带外注入给
# 工具的 _read_files 参数——每个 Agent（含子 agent）各有一套，天然隔离、reset 即清空。
# 不用模块级全局集合：那会被所有调用共享、进程内长存累积，正是要消灭的 bug。
# 若脱离 Agent 直接调工具（如单测）而不传 _read_files，则每次调用用一个**一次性空集合**
# （读了就登记进它、调用返回即丢），不跨调用累积、不污染——想让 read+write 共享状态就显式传同一个集合。


@tool({"path": "文件路径，可以是相对或绝对路径"}, concurrency_safe=True)
def read_file(path: str, _read_files: set | None = None) -> str:
    """读取一个文本文件的完整内容。当你需要查看某个文件写了什么时使用。"""
    if not isinstance(path, str):
        return tool_error("[错误] read_file.path 必须是字符串")
    # 错误不抛异常，而是返回可读字符串 —— 这样错误会进入模型上下文，
    # 模型能据此自我纠正（第 3.2 节「让 LLM 自己解决问题」）。
    # _read_files 是带外注入的实例集合（不进 schema）；None 时用一次性空集合（不跨调用累积）。
    read_files = _read_files if _read_files is not None else set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        read_files.add(_norm(path))  # 登记：这个文件被读过了
        return tool_success(content)
    except FileNotFoundError:
        return tool_error(f"[错误] 文件不存在：{path}")
    except Exception as e:  # noqa: BLE001 —— 教学项目，统一兜底成可读错误
        return tool_error(f"[错误] 读取失败：{path} —— {e}")


@tool({"command": "要执行的完整 shell 命令，如 'ls -la'"})
def run_command(command: str, _timeout: int = 30) -> str:
    """执行一条 shell 命令并返回输出。用于列目录、查找文件、运行程序等。注意：Windows 环境，底层为 cmd.exe。"""
    if not isinstance(command, str) or not isinstance(_timeout, int) or isinstance(_timeout, bool):
        return tool_error("[错误] run_command.command 必须是字符串，timeout 必须是整数")
    # 注意：Phase 1/2 直接执行、无沙盒、无权限控制 —— 安全护栏留到 Phase 4 harness。
    # Windows 坑（Phase 1 记录）：shell=True 底层是 cmd.exe；某些命令（date/time）会等输入，
    # 故 stdin=DEVNULL 让它秒退不挂；中文输出是 GBK，故指定 encoding + errors="replace"。
    # _timeout（下划线=带外注入，不进 schema、模型看不到）：默认 30s。验证门跑慢测试套件时
    # 由调用方传更大值（审查 #4：原先硬编码 30s，>30s 的 pytest 套件会被杀、误判失败）。
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=_timeout,
            stdin=subprocess.DEVNULL,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip() or "[命令无输出]"
        if result.returncode != 0:
            return tool_error(f"[错误] 命令退出码 {result.returncode}：{output}")
        return tool_success(output)
    except subprocess.TimeoutExpired:
        return tool_error(f"[错误] 命令超时（>{_timeout}s）：{command}（可能在等待输入，或确实耗时过长）")
    except Exception as e:  # noqa: BLE001
        return tool_error(f"[错误] 命令执行失败：{command} —— {e}")


def run_command_with_exit(command: str, timeout: int = 30) -> tuple[int | None, str]:
    """跑命令并返回 (退出码, 输出)。供验证门用——退出码是命令成败的**唯一可靠判据**。

    审查 #3：run_command 只回字符串、吞掉 returncode，验证门被迫靠文本猜成败（把含
    `test_error_x`/`0 errors` 的合格输出误判失败、把无关键词的真失败误判通过）。这里保留
    退出码，让验证门优先按它判定。退出码为 None 表示命令根本没跑起来（超时/异常），
    验证门应视为「未通过/不确定」。timeout 可配（同 #4）。
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip() or "[命令无输出]"
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return None, f"[错误] 命令超时（>{timeout}s）：{command}（未跑完，无法判定成败）"
    except Exception as e:  # noqa: BLE001
        return None, f"[错误] 命令执行失败：{command} —— {e}"


@tool({"path": "要写入的文件路径", "content": "要写入文件的完整内容"})
def write_file(path: str, content: str, _read_files: set | None = None) -> str:
    """把内容写入文件（覆盖原内容）。**必须先用 read_file 读过该文件才能写**（防止盲改）。"""
    if not isinstance(path, str) or not isinstance(content, str):
        return tool_error("[错误] write_file.path/content 必须是字符串")
    # 「先读再改」硬约束（对照 Claude Code 第 15.2 节 FileEditTool）：
    # 没读过的已存在文件，禁止写 —— 防止模型基于「想象的内容」盲目覆盖。
    # 这是本项目第一个真正的 harness 约束：用代码强制，不靠模型自律。
    # _read_files 是带外注入的实例集合（不进 schema）；None 时用一次性空集合（不跨调用累积）。
    import os
    read_files = _read_files if _read_files is not None else set()
    norm = _norm(path)
    if os.path.exists(path) and norm not in read_files:
        return tool_error(
            f"[拒绝] 文件已存在但你还没读过它：{path}。"
            f"请先用 read_file 读取，确认当前内容后再写，避免盲目覆盖。"
        )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        read_files.add(norm)  # 写完也算「知道内容」，之后可再改
        return tool_success(f"[成功] 已写入 {path}（{len(content)} 字符）")
    except Exception as e:  # noqa: BLE001
        return tool_error(f"[错误] 写入失败：{path} —— {e}")


# ─────────────────────────────────────────────────────────────
# 2. 分发器 + 小工具
# ─────────────────────────────────────────────────────────────

def is_concurrency_safe(name: str) -> bool:
    """工具是否可与同轮其它工具并发执行（P1）。未知工具**保守当不安全**（默认串行）。

    主循环据此把一轮里的工具分批：安全的（只读，如 read_file）并发跑，不安全的
    （有副作用，如 write_file/run_command/spawn_subagent）按模型给出的原始顺序串行跑——
    避免同轮并行写同一文件的竞态（TODO P1）。
    """
    return TOOL_CONCURRENCY_SAFE.get(name, False)


def tool_schemas_for(names: set[str]) -> list:
    """按工具名白名单返回同一注册版本的独立 schema 快照。"""
    with _TOOL_REGISTRY_LOCK:
        known = {schema["name"] for schema in TOOL_SCHEMAS}
        unknown = names - known
        if unknown:
            raise ValueError(f"未知工具：{', '.join(sorted(unknown))}")
        selected = [
            copy.deepcopy(schema)
            for schema in TOOL_SCHEMAS
            if schema["name"] in names
        ]
        if len(selected) != len(names):
            raise ValueError("全局工具注册表含重复名称，无法建立唯一白名单")
        return selected


def bind_tool_schemas(schemas: list | None = None) -> tuple[list, dict, dict, set[str]]:
    """原子绑定 schema、handler、并发属性；拒绝缓存后已过期的 schema。

    ``schemas=None`` 表示绑定当前全局全集。显式 schema 必须与注册表当前版本完全一致，
    避免 Notebook/reload 热替换后出现“旧说明书 + 新 handler”。
    """
    with _TOOL_REGISTRY_LOCK:
        current = {item["name"]: item for item in TOOL_SCHEMAS}
        if len(current) != len(TOOL_SCHEMAS):
            raise ValueError("全局工具注册表含重复名称")
        selected = (
            copy.deepcopy(TOOL_SCHEMAS)
            if schemas is None else copy.deepcopy(list(schemas))
        )
        names = [item.get("name") for item in selected]
        if any(not isinstance(name, str) for name in names):
            raise ValueError("基础工具 schema 必须包含字符串 name")
        if len(names) != len(set(names)):
            raise ValueError("基础工具名不能重复")
        missing = set(names) - set(current)
        if missing:
            raise ValueError(f"基础工具缺少已注册 handler：{', '.join(sorted(missing))}")
        stale = [name for name, schema in zip(names, selected) if schema != current[name]]
        if stale:
            raise ValueError(f"工具 schema 已过期，请重新获取：{', '.join(sorted(stale))}")
        handlers = {name: TOOL_FUNCTIONS[name] for name in names}
        concurrency = {name: TOOL_CONCURRENCY_SAFE.get(name, False) for name in names}
        return selected, handlers, concurrency, set(current)


def subagent_tool_schemas() -> list:
    """返回普通子 Agent 的固定基础能力，不让后注册工具自动扩权。

    对照 agent_learning 第 9.7 节：只允许一层派生。正向白名单不仅排除
    ``spawn_subagent``，也防止未来新增发布/删除类工具后悄悄泄漏给所有子 Agent。
    """
    return tool_schemas_for({"read_file", "run_command", "write_file"})


def _norm(path: str) -> str:
    """把路径归一化成绝对路径，作为「已读文件」集合的键（避免相对/绝对不一致）。"""
    import os
    return os.path.normcase(os.path.abspath(path))


def execute_tool(
    name: str,
    tool_input: dict,
    read_files: set | None = None,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    parent_trace: str | None = None,
) -> ToolOutput:
    """按名分发工具，统一返回带 ``is_error`` 的字符串兼容结果。

    下划线参数是执行上下文依赖，不进入模型 schema。``spawn_subagent`` 用它继承父 Agent
    的模型、输出上限和 trace 血缘，避免显式配置在派生边界静默漂移。
    """
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return tool_error(f"[错误] 未知工具：{name}")

    signature = inspect.signature(func)
    call_kwargs = {
        key: copy.deepcopy(value)
        for key, value in tool_input.items()
        if not str(key).startswith("_")
    }
    injected = {
        "_read_files": read_files,
        "_model": model,
        "_max_tokens": max_tokens,
        "_parent_trace": parent_trace,
    }
    for parameter, value in injected.items():
        if value is not None and parameter in signature.parameters:
            call_kwargs[parameter] = value
    try:
        # 先显式绑定，避免把 handler 函数体内部的 TypeError 误报成“模型参数不对”。
        signature.bind(**call_kwargs)
    except TypeError as e:
        return tool_error(
            f"[错误] 工具 {name} 参数不对：{e}（请检查参数名/类型是否与工具说明一致）"
        )

    try:
        result = func(**call_kwargs)
    except Exception as e:  # noqa: BLE001 —— 工具错误回喂，让模型自我纠正
        return tool_error(f"[错误] 工具 {name} 执行异常：{e}")
    if isinstance(result, ToolOutput):
        return result
    if isinstance(result, str):
        # 旧字符串没有可靠机器状态；按成功处理。失败工具应迁移为显式 tool_error。
        return tool_success(result)
    return tool_error(
        f"[错误] 工具 {name} 返回类型不合法：{type(result).__name__}（应返回字符串或 ToolOutput）"
    )
