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

import inspect
import locale
import subprocess

# ─────────────────────────────────────────────────────────────
# 0. @tool 装饰器 + 自动注册表
# ─────────────────────────────────────────────────────────────
# 两张表在 import 时由装饰器自动填充：
#   TOOL_FUNCTIONS：工具名 → 真正的 Python 函数（供循环层按名分发）
#   TOOL_SCHEMAS  ：给模型看的说明书列表（Anthropic 的 name/description/input_schema）
TOOL_FUNCTIONS: dict = {}
TOOL_SCHEMAS: list = []

# Python 类型注解 → JSON Schema 类型的简单映射。
_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


def tool(param_desc: dict | None = None):
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
    """
    param_desc = param_desc or {}

    def decorator(func):
        sig = inspect.signature(func)
        properties = {}
        required = []
        for pname, p in sig.parameters.items():
            json_type = _PY_TO_JSON.get(p.annotation, "string")  # 注解缺失就当字符串
            properties[pname] = {
                "type": json_type,
                "description": param_desc.get(pname, ""),
            }
            if p.default is inspect.Parameter.empty:  # 无默认值 → 必填
                required.append(pname)

        # docstring 第一段作为工具描述（去掉缩进和空行）。
        doc = inspect.getdoc(func) or ""
        description = doc.split("\n\n")[0].strip()

        TOOL_SCHEMAS.append({
            "name": func.__name__,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
        TOOL_FUNCTIONS[func.__name__] = func
        return func

    return decorator


# ─────────────────────────────────────────────────────────────
# 1. 工具函数：真正干活的「手」（加 @tool 即自动注册 + 生成 schema）
# ─────────────────────────────────────────────────────────────

# 记录「本会话读过哪些文件」，供 write_file 的「先读再改」约束检查。
# 由 agent 层在执行 read_file 后登记（见 agent.py）。这里用集合存绝对路径。
READ_FILES: set = set()


@tool({"path": "文件路径，可以是相对或绝对路径"})
def read_file(path: str) -> str:
    """读取一个文本文件的完整内容。当你需要查看某个文件写了什么时使用。"""
    # 错误不抛异常，而是返回可读字符串 —— 这样错误会进入模型上下文，
    # 模型能据此自我纠正（第 3.2 节「让 LLM 自己解决问题」）。
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        READ_FILES.add(_norm(path))  # 登记：这个文件被读过了
        return content
    except FileNotFoundError:
        return f"[错误] 文件不存在：{path}"
    except Exception as e:  # noqa: BLE001 —— 教学项目，统一兜底成可读错误
        return f"[错误] 读取失败：{path} —— {e}"


@tool({"command": "要执行的完整 shell 命令，如 'ls -la'"})
def run_command(command: str) -> str:
    """执行一条 shell 命令并返回输出。用于列目录、查找文件、运行程序等。注意：Windows 环境，底层为 cmd.exe。"""
    # 注意：Phase 1/2 直接执行、无沙盒、无权限控制 —— 安全护栏留到 Phase 4 harness。
    # Windows 坑（Phase 1 记录）：shell=True 底层是 cmd.exe；某些命令（date/time）会等输入，
    # 故 stdin=DEVNULL 让它秒退不挂；中文输出是 GBK，故指定 encoding + errors="replace"。
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output.strip() or "[命令无输出]"
    except subprocess.TimeoutExpired:
        return f"[错误] 命令超时（>30s）：{command}（可能在等待输入，或确实耗时过长）"
    except Exception as e:  # noqa: BLE001
        return f"[错误] 命令执行失败：{command} —— {e}"


@tool({"path": "要写入的文件路径", "content": "要写入文件的完整内容"})
def write_file(path: str, content: str) -> str:
    """把内容写入文件（覆盖原内容）。**必须先用 read_file 读过该文件才能写**（防止盲改）。"""
    # 「先读再改」硬约束（对照 Claude Code 第 15.2 节 FileEditTool）：
    # 没读过的已存在文件，禁止写 —— 防止模型基于「想象的内容」盲目覆盖。
    # 这是本项目第一个真正的 harness 约束：用代码强制，不靠模型自律。
    import os
    norm = _norm(path)
    if os.path.exists(path) and norm not in READ_FILES:
        return (f"[拒绝] 文件已存在但你还没读过它：{path}。"
                f"请先用 read_file 读取，确认当前内容后再写，避免盲目覆盖。")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        READ_FILES.add(norm)  # 写完也算「知道内容」，之后可再改
        return f"[成功] 已写入 {path}（{len(content)} 字符）"
    except Exception as e:  # noqa: BLE001
        return f"[错误] 写入失败：{path} —— {e}"


# ─────────────────────────────────────────────────────────────
# 2. 分发器 + 小工具
# ─────────────────────────────────────────────────────────────

def subagent_tool_schemas() -> list:
    """返回给子 agent 用的**受限工具集**：全集里剔除 spawn_subagent 本身。

    为什么剔除（对照第 9.7 节 + P4 防失控精神）：如果子 agent 也能 spawn_subagent，
    它可能再派生子子 agent，层层嵌套失控烧钱。**只允许主 agent 派生、子 agent 只干活**——
    一层派生，不嵌套。子 agent 拿到的是 read_file / run_command / write_file 这些基础工具。

    注：spawn_subagent 工具本身定义在 agent.py（它需要 Agent，放那儿避免 tools→agent 循环导入，
    见 agent.py 里 spawn_subagent 的注释）。只有 import 了 agent.py，它才注册进 TOOL_SCHEMAS；
    所以这里用「名字过滤」而非硬编码列表，agent 导没导入都能正确工作。
    """
    return [s for s in TOOL_SCHEMAS if s["name"] != "spawn_subagent"]


def _norm(path: str) -> str:
    """把路径归一化成绝对路径，作为 READ_FILES 的键（避免相对/绝对不一致）。"""
    import os
    return os.path.normcase(os.path.abspath(path))


def execute_tool(name: str, tool_input: dict) -> str:
    """按名分发到真正的函数并执行。找不到就返回可读错误。"""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"[错误] 未知工具：{name}"
    return func(**tool_input)
