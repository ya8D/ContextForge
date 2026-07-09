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
    tools.READ_FILES.discard(_norm(str(f)))  # 先确保没登记
    read_file(str(f))
    assert _norm(str(f)) in tools.READ_FILES  # 读后应被登记


# ── write_file 的「先读再改」硬约束（重点）────────────────────

def test_write_existing_file_without_reading_is_rejected(tmp_path):
    f = tmp_path / "exist.txt"
    f.write_text("原内容", encoding="utf-8")
    tools.READ_FILES.discard(_norm(str(f)))  # 确保「没读过」
    result = write_file(str(f), "新内容")
    assert result.startswith("[拒绝]")
    assert f.read_text(encoding="utf-8") == "原内容"  # 文件未被改动


def test_write_after_reading_succeeds(tmp_path):
    f = tmp_path / "exist2.txt"
    f.write_text("原内容", encoding="utf-8")
    read_file(str(f))                       # 先读（登记进 READ_FILES）
    result = write_file(str(f), "新内容")
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


# ── execute_tool 分发 ────────────────────────────────────────

def test_execute_tool_dispatches_known():
    result = execute_tool("run_command", {"command": "echo dispatch_ok"})
    assert "dispatch_ok" in result


def test_execute_tool_unknown_returns_error():
    result = execute_tool("no_such_tool", {})
    assert result.startswith("[错误]")
    assert "未知工具" in result
