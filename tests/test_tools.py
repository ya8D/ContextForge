"""
test_tools.py —— tools.py 的纯逻辑测试（不调 API，不烧钱，毫秒级）。

覆盖：@tool 装饰器生成的 schema、read_file、write_file 的「先读再改」约束、
run_command 的错误兜底、execute_tool 分发。

跑法：
    py -m pytest tests/test_tools.py -v
    py -m pytest -m "not e2e"           # 跑所有非端到端测试
"""

from contextforge import tools
from contextforge.tools import (
    TOOL_SCHEMAS,
    execute_tool,
    read_file,
    run_command,
    subagent_tool_schemas,
    write_file,
    _norm,
)


# ── @tool 装饰器自动生成的 schema ──────────────────────────────

def test_tools_module_defines_base_tools():
    """tools.py 自身定义的 3 个基础工具都注册了。

    P5.1 起 spawn_subagent 移到了 agent.py（解循环导入），它导入 agent 后才注册进
    TOOL_SCHEMAS。所以这里用**子集**断言「基础工具都在」，不写死总数 ——
    否则会依赖「agent 有没有被 import」这个全局状态（pytest 跑全套时 test_subagent
    会 import agent，导致 spawn 已注册），断言总数就脆了。
    """
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert {"read_file", "run_command", "write_file"} <= names


def test_subagent_tool_schemas_excludes_spawn():
    """子 agent 的受限工具集必须剔除 spawn_subagent（防无限递归派生）。"""
    sub_names = {t["name"] for t in subagent_tool_schemas()}
    assert "spawn_subagent" not in sub_names
    # 基础工具仍在
    assert {"read_file", "run_command", "write_file"} <= sub_names


def test_schema_fields_correct():
    by_name = {t["name"]: t for t in TOOL_SCHEMAS}
    rf = by_name["read_file"]
    # 装饰器应从函数签名生成 input_schema
    assert rf["input_schema"]["type"] == "object"
    assert "path" in rf["input_schema"]["properties"]
    assert rf["input_schema"]["required"] == ["path"]  # path 无默认值 → 必填
    assert rf["description"]                            # docstring 变成非空描述
    # write_file 有两个必填参数
    wf = by_name["write_file"]
    assert set(wf["input_schema"]["required"]) == {"path", "content"}


# ── read_file ────────────────────────────────────────────────

def test_read_existing_file(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("你好世界", encoding="utf-8")
    result = read_file(str(f))
    assert result == "你好世界"


def test_read_missing_file_returns_error_not_raise(tmp_path):
    # 不存在的文件应返回可读错误字符串，而不是抛异常
    result = read_file(str(tmp_path / "nope.txt"))
    assert result.startswith("[错误]")
    assert "不存在" in result


def test_read_registers_in_read_files(tmp_path):
    f = tmp_path / "reg.txt"
    f.write_text("x", encoding="utf-8")
    rf: set = set()                       # 显式传入自己的集合（不靠模块全局）
    read_file(str(f), _read_files=rf)
    assert _norm(str(f)) in rf            # 读后应被登记进这个集合


# ── write_file 的「先读再改」硬约束（重点）────────────────────

def test_write_existing_file_without_reading_is_rejected(tmp_path):
    f = tmp_path / "exist.txt"
    f.write_text("原内容", encoding="utf-8")
    rf: set = set()                       # 空集合 = 没读过任何文件
    result = write_file(str(f), "新内容", _read_files=rf)
    assert result.startswith("[拒绝]")
    assert f.read_text(encoding="utf-8") == "原内容"  # 文件未被改动


def test_write_after_reading_succeeds(tmp_path):
    f = tmp_path / "exist2.txt"
    f.write_text("原内容", encoding="utf-8")
    rf: set = set()
    read_file(str(f), _read_files=rf)              # 先读（登记进 rf）
    result = write_file(str(f), "新内容", _read_files=rf)  # 同一个集合 → 放行
    assert result.startswith("[成功]")
    assert f.read_text(encoding="utf-8") == "新内容"


def test_write_brand_new_file_succeeds(tmp_path):
    # 全新文件（不存在）不受「先读再改」约束，直接允许写
    f = tmp_path / "brand_new.txt"
    result = write_file(str(f), "内容")
    assert result.startswith("[成功]")
    assert f.read_text(encoding="utf-8") == "内容"


# ── run_command 的错误兜底（不抛异常）────────────────────────

def test_run_command_basic():
    result = run_command("echo hello")
    assert "hello" in result


def test_run_command_bad_command_returns_error_not_raise():
    # 不存在的命令应返回可读文本，而不是抛异常让 agent 崩溃
    result = run_command("this_command_definitely_does_not_exist_xyz")
    assert isinstance(result, str)  # 返回字符串（可能是错误信息或 shell 的报错）


# ── 带外注入的 _read_files 不进 schema + 实例隔离 ─────────────

def test_underscore_param_not_in_schema():
    """下划线前缀参数（_read_files）是带外注入的实例状态，不该出现在给模型的 input_schema 里。"""
    schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "read_file")
    props = schema["input_schema"]["properties"]
    assert "path" in props
    assert "_read_files" not in props            # 模型看不到它
    ws = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "write_file")
    assert "_read_files" not in ws["input_schema"]["properties"]


def test_read_files_isolation_via_injected_set(tmp_path):
    """带外注入不同的 _read_files 集合 → 「已读」状态互相隔离（模拟两个 Agent / 主子 agent）。"""
    f = tmp_path / "iso.txt"
    f.write_text("x", encoding="utf-8")
    set_a: set = set()
    set_b: set = set()
    read_file(str(f), _read_files=set_a)          # 只有 A 读了
    assert _norm(str(f)) in set_a
    assert _norm(str(f)) not in set_b             # B 不受影响（隔离）
    # 于是同一个文件：用 A 的集合写放行，用 B 的集合写被拒（B 没读过）
    assert write_file(str(f), "改", _read_files=set_a).startswith("[成功]")
    f.write_text("x", encoding="utf-8")
    assert write_file(str(f), "改", _read_files=set_b).startswith("[拒绝]")


def test_execute_tool_injects_read_files(tmp_path):
    """execute_tool 把带外 read_files 注入给 read_file/write_file，进而满足「先读再改」。"""
    f = tmp_path / "inj.txt"
    f.write_text("原", encoding="utf-8")
    rf: set = set()
    execute_tool("read_file", {"path": str(f)}, read_files=rf)      # 经 execute_tool 读
    assert _norm(str(f)) in rf                                       # 注入的集合被登记
    assert execute_tool("write_file", {"path": str(f), "content": "新"}, read_files=rf).startswith("[成功]")


def test_execute_tool_bad_param_name_returns_error_not_raise():
    """模型幻觉出错误参数名（read_file(filename=...) 而非 path=）→ 返回可读错误、不抛崩溃。"""
    result = execute_tool("read_file", {"filename": "x.txt"})       # 错误参数名
    assert isinstance(result, str)
    assert "参数不对" in result


# ── execute_tool 分发 ────────────────────────────────────────

def test_execute_tool_dispatches_known():
    result = execute_tool("run_command", {"command": "echo dispatch_ok"})
    assert "dispatch_ok" in result


def test_execute_tool_unknown_returns_error():
    result = execute_tool("no_such_tool", {})
    assert result.startswith("[错误]")
    assert "未知工具" in result


# ── P1：工具并发安全标记 ──

def test_read_file_marked_concurrency_safe():
    """read_file 是只读工具，标记为并发安全。"""
    from contextforge.tools import is_concurrency_safe
    assert is_concurrency_safe("read_file") is True


def test_side_effecting_tools_not_concurrency_safe():
    """write_file / run_command 有副作用，默认不并发安全（同轮串行）。"""
    from contextforge.tools import is_concurrency_safe
    assert is_concurrency_safe("write_file") is False
    assert is_concurrency_safe("run_command") is False


def test_unknown_tool_defaults_not_concurrency_safe():
    """未知工具保守当不安全（默认串行兜底）。"""
    from contextforge.tools import is_concurrency_safe
    assert is_concurrency_safe("no_such_tool") is False


def test_concurrency_safe_flag_not_in_schema():
    """concurrency_safe 是执行调度用的元信息，不该出现在给模型的 input_schema 里。"""
    rf = next(s for s in TOOL_SCHEMAS if s["name"] == "read_file")
    assert "concurrency_safe" not in rf["input_schema"]["properties"]


# ── 结构化工具结果：正文与执行状态分离 ────────────────────────

def test_read_file_success_does_not_guess_status_from_content(tmp_path):
    """合法正文即使以错误标记开头，读取仍必须是机器可读的成功结果。"""
    path = tmp_path / "error_log.txt"
    path.write_text("[错误] 这是文件正文，不是工具失败", encoding="utf-8")

    result = read_file(str(path))

    assert isinstance(result, tools.ToolOutput)
    assert result == "[错误] 这是文件正文，不是工具失败"
    assert result.is_error is False


def test_missing_file_returns_machine_readable_error(tmp_path):
    """文件不存在不能只靠中文前缀表达，控制面必须能直接读取 is_error。"""
    result = read_file(str(tmp_path / "missing.txt"))

    assert isinstance(result, tools.ToolOutput)
    assert result.is_error is True


def test_run_command_nonzero_exit_is_machine_readable_error():
    """子进程正常启动但退出码非零仍是失败，不能被“有输出/无输出”文本掩盖。"""
    result = run_command('py -c "import sys; sys.exit(7)"')

    assert isinstance(result, tools.ToolOutput)
    assert result.is_error is True
    assert "退出码 7" in result


def test_execute_tool_unknown_is_machine_readable_error():
    """分发层错误也必须沿用同一结构化结果协议。"""
    result = execute_tool("no_such_structured_tool", {})

    assert isinstance(result, tools.ToolOutput)
    assert result.is_error is True
