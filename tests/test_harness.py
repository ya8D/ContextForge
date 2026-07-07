"""
test_harness.py —— P4 Harness 约束的纯逻辑测试（不烧钱、毫秒级）。

harness.py 三根柱子全是纯逻辑（正则匹配、滑动窗口、判据检查），
副作用（跑检查命令）通过假 runner 回调隔离。所以整份测试零 API 调用。

跑法：py -m pytest tests/test_harness.py -v （属于 "not e2e" 那批）
"""

import harness as h
from harness import (
    LoopDetector,
    PermissionLevel,
    ValidationGate,
    check_command_safety,
    check_path_safety,
    check_test_deletion,
    check_tool_call,
)


# ── 支柱① 权限分级 + 危险动作拦截 ──

def test_permission_levels_ordered():
    """权限级别数字递增（越大越危险），且 3 个工具都登记了。"""
    assert PermissionLevel.READ_ONLY.value < PermissionLevel.WRITE_SAFE.value
    assert PermissionLevel.WRITE_SAFE.value < PermissionLevel.WRITE_DESTRUCTIVE.value
    assert h.TOOL_PERMISSIONS["read_file"] == PermissionLevel.READ_ONLY
    assert h.TOOL_PERMISSIONS["run_command"] == PermissionLevel.WRITE_DESTRUCTIVE


def test_dangerous_commands_blocked():
    """各类危险命令都被拦（返回 False）。"""
    for cmd in ["rm -rf /", "rm -r foo", "del /s /q C:\\data",
                "format d:", "mkfs.ext4 /dev/sda", "shutdown now",
                "chmod -R 777 /"]:
        ok, reason = check_command_safety(cmd)
        assert ok is False, f"危险命令未被拦：{cmd}"


def test_safe_commands_allowed():
    """常见安全命令放行（返回 True）。"""
    for cmd in ["ls -la", "echo hello", "cat foo.txt", "python script.py",
                "git status", "pytest -q"]:
        ok, _ = check_command_safety(cmd)
        assert ok is True, f"安全命令被误拦：{cmd}"


def test_path_traversal_blocked():
    """路径遍历（..）和系统目录被拦。"""
    assert check_path_safety("../etc/passwd")[0] is False
    assert check_path_safety("foo/../../bar")[0] is False
    assert check_path_safety("/etc/hosts")[0] is False
    assert check_path_safety("/usr/bin/x")[0] is False
    assert check_path_safety("C:\\Windows\\system32\\x")[0] is False


def test_normal_paths_allowed():
    """项目内正常路径放行。"""
    assert check_path_safety("myagent/foo.txt")[0] is True
    assert check_path_safety("./notes.md")[0] is True
    assert check_path_safety("C:/AI_learning/myagent/x.py")[0] is True


def test_check_tool_call_routing():
    """统一关卡按工具类型路由到对应检查。"""
    # run_command 走命令检查
    assert check_tool_call("run_command", {"command": "rm -rf /"})[0] is False
    assert check_tool_call("run_command", {"command": "ls"})[0] is True
    # write_file 走路径检查
    assert check_tool_call("write_file", {"path": "../x", "content": "y"})[0] is False
    assert check_tool_call("write_file", {"path": "ok.txt", "content": "y"})[0] is True
    # read_file 只读，天然放行
    assert check_tool_call("read_file", {"path": "anything"})[0] is True


# ── 支柱② 死循环检测 ──

def test_loop_detector_triggers_on_repeats():
    """连续 max_same 次完全相同 action → 判定死循环。"""
    ld = LoopDetector(max_same=3)
    assert ld.is_looping() is False  # 空的
    ld.record("run_command", {"command": "echo x"})
    ld.record("run_command", {"command": "echo x"})
    assert ld.is_looping() is False  # 才 2 次，不够
    ld.record("run_command", {"command": "echo x"})
    assert ld.is_looping() is True  # 第 3 次相同 → 触发


def test_loop_detector_resets_on_different_action():
    """换了 action 后不再判定为循环（滑动窗口混入不同指纹）。"""
    ld = LoopDetector(max_same=3)
    for _ in range(3):
        ld.record("run_command", {"command": "echo x"})
    assert ld.is_looping() is True
    # 换个动作，窗口里不再全相同
    ld.record("read_file", {"path": "a.txt"})
    assert ld.is_looping() is False


def test_loop_detector_param_order_insensitive():
    """参数字典顺序不影响指纹（{a,b} 和 {b,a} 算同一个 action）。"""
    ld = LoopDetector(max_same=2)
    ld.record("t", {"a": 1, "b": 2})
    ld.record("t", {"b": 2, "a": 1})  # 顺序不同但内容相同
    assert ld.is_looping() is True


def test_loop_detector_reset():
    """reset 后清零。"""
    ld = LoopDetector(max_same=2)
    ld.record("t", {"x": 1})
    ld.record("t", {"x": 1})
    assert ld.is_looping() is True
    ld.reset()
    assert ld.is_looping() is False


# ── 支柱② record_round：整轮指纹（P4.1 修正，钉死两个误判 + reset 洞）──

class _FakeBlock:
    """模拟 SDK 的 tool_use block（record_round 只读 .name / .input）。"""
    def __init__(self, name, tool_input):
        self.name = name
        self.input = tool_input


def _round(*pairs):
    """便捷造一整轮的 blocks：_round(("read_file",{"path":"A"}), ...)。"""
    return [_FakeBlock(name, inp) for name, inp in pairs]


def test_record_round_detects_multitool_loop_regardless_of_order():
    """方向 A（漏报修复）：多工具**乱序**的真循环也要判出。

    本轮 [读A,读B]、下轮 [读B,读A]、再 [读A,读B]——顺序在变但就是同一组动作，
    整轮+排序后指纹相同 → 连续 3 轮判定循环。（只看 [0] 会漏报。）
    """
    ld = LoopDetector(max_same=3)
    ld.record_round(_round(("read_file", {"path": "A"}), ("read_file", {"path": "B"})))
    ld.record_round(_round(("read_file", {"path": "B"}), ("read_file", {"path": "A"})))  # 乱序
    ld.record_round(_round(("read_file", {"path": "A"}), ("read_file", {"path": "B"})))
    assert ld.is_looping() is True


def test_record_round_no_false_positive_when_round_progresses():
    """方向 B（误报修复）：第一个工具连续相同、但整轮在推进 → 不该判循环。

    [跑测试,读日志] → [跑测试,写报告] → [跑测试,提交]，第一个工具都是 run_command 跑测试，
    但整轮各不同（在推进）→ 整轮指纹不同 → 不判循环。（只看 [0] 会误报打断。）
    """
    ld = LoopDetector(max_same=3)
    ld.record_round(_round(("run_command", {"command": "pytest"}), ("read_file", {"path": "log"})))
    ld.record_round(_round(("run_command", {"command": "pytest"}), ("write_file", {"path": "report"})))
    ld.record_round(_round(("run_command", {"command": "pytest"}), ("run_command", {"command": "git commit"})))
    assert ld.is_looping() is False


def test_record_round_keeps_triggering_without_auto_reset():
    """reset 洞修复：不听劝、连续重复同一整轮 → **每轮都**判循环，不会自动清零放水。

    这是 P4.1 的核心修正：命中后 agent 不再 reset，所以第 4、5 轮继续重复时，
    is_looping 仍持续为 True（一次不漏），而不是「拦一次→清零→再放 2 轮」。
    """
    ld = LoopDetector(max_same=3)
    r = lambda: _round(("run_command", {"command": "echo x"}))
    ld.record_round(r()); ld.record_round(r()); ld.record_round(r())
    assert ld.is_looping() is True   # 第 3 轮触发
    ld.record_round(r())
    assert ld.is_looping() is True   # 第 4 轮仍触发（没被自动清零）
    ld.record_round(r())
    assert ld.is_looping() is True   # 第 5 轮仍触发


def test_record_round_param_order_insensitive():
    """整轮里单个工具的参数字典顺序也不影响指纹（复用 _fingerprint 的排序）。"""
    ld = LoopDetector(max_same=2)
    ld.record_round(_round(("t", {"a": 1, "b": 2})))
    ld.record_round(_round(("t", {"b": 2, "a": 1})))
    assert ld.is_looping() is True


# ── 支柱③ 验证门 + 防作弊 ──

def test_check_test_deletion_flags_gutting():
    """删测试远多于加 → 判定可疑（掏空测试作弊）。"""
    suspicious, _ = check_test_deletion("tests/test_foo.py", lines_added=1, lines_deleted=40)
    assert suspicious is True


def test_check_test_deletion_ignores_non_test():
    """非测试文件不管。"""
    suspicious, _ = check_test_deletion("src/main.py", lines_added=1, lines_deleted=40)
    assert suspicious is False


def test_check_test_deletion_allows_normal_edit():
    """测试文件正常增改（删得不多）不算作弊。"""
    suspicious, _ = check_test_deletion("tests/test_foo.py", lines_added=10, lines_deleted=3)
    assert suspicious is False


def test_validation_gate_no_command_passes():
    """没配检查命令 → 无条件放行（纯问答不添乱）。"""
    gate = ValidationGate(check_command=None)
    passed, _ = gate.verify(lambda cmd: "不该被调用")
    assert passed is True


def test_validation_gate_passes_on_clean_output():
    """配了命令、输出干净（无 fail/error）→ 通过。用假 runner 不烧钱。"""
    gate = ValidationGate(check_command="pytest -q")
    passed, report = gate.verify(lambda cmd: "5 passed in 0.1s")
    assert passed is True
    assert "验证通过" in report


def test_validation_gate_fails_on_error_output():
    """配了命令、输出含失败关键词 → 不通过、打回。"""
    gate = ValidationGate(check_command="pytest -q")
    passed, report = gate.verify(lambda cmd: "1 failed, 4 passed\nFAILED test_x")
    assert passed is False
    assert "未通过" in report


def test_validation_gate_runner_receives_command():
    """验证门确实把配置的检查命令传给了 runner。"""
    captured = {}

    def capturing_runner(cmd: str) -> str:
        captured["cmd"] = cmd
        return "ok passed"

    gate = ValidationGate(check_command="py -m pytest -q")
    gate.verify(capturing_runner)
    assert captured["cmd"] == "py -m pytest -q"
