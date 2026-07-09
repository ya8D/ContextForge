"""
harness.py —— Harness 约束（给 TAOR 循环套护栏，对照 agent_learning 第 8 章）

前面 P1-P3 让 agent「能干活、不撑爆」。但它还太天真：可能一个 rm -rf 删掉东西、
可能卡在同一个错误上反复调同一命令、可能嘴上说「完成了」其实测试没跑过甚至偷删了测试。

本模块实现书里六大支柱中**前三根**（书明说这三根解决 80% 可靠性问题）：
  ① 权限分级（支柱二 架构约束）：工具分级 + 危险命令/路径遍历拦截。用代码强制，不靠模型自律。
  ② 死循环检测（支柱三配套）：连续 N 次相同 action 就判定鬼打墙，注入「换思路」提示。
  ③ 验证门（支柱三 自验证循环）：声称完成前强制过检查命令，没过就打回——对治 LLM 的完成偏见。

★ 本层最关键的认知：这些约束都发生在**我们的代码里**，卡在「模型请求 → 真正执行」之间。
  模型只是「请求」调某工具，批不批、拦不拦，是 harness 说了算。这就是「harness 包着 agent」。

对照 agent_learning：第 8.1（什么是 Harness）、8.2（六大支柱）、8.5（构建 Harness）。

与书的差异（贴合本项目单 agent 场景）：
  书里的 ToolRegistry 按「角色」预筛工具集（多 agent 场景：reviewer/writer/devops 各开不同工具）。
  我们是单 agent、工具就 3 个，真正的风险不是「该不该给它 run_command」，而是
  「它用 run_command 跑了个 rm -rf」。所以我们做**运行时拦截**（执行前检查危险动作），
  而非预筛工具列表 —— 对单 agent 更实在。
"""

import re
from enum import Enum


# ─────────────────────────────────────────────────────────────
# 支柱① 权限分级 + 危险动作拦截
# ─────────────────────────────────────────────────────────────

class PermissionLevel(Enum):
    """工具权限级别（对照书 8.2 支柱二）。数字越大越危险。"""
    READ_ONLY = 1          # 只读，最安全（read_file）
    WRITE_SAFE = 2         # 可写，但可控（write_file，且有先读再改约束）
    WRITE_DESTRUCTIVE = 3  # 可能删/改重要数据，需拦截或确认（run_command 能跑任意命令）


# 每个工具的权限级别。run_command 能跑任意 shell → 最危险。
# 注意：这是「工具级」的粗分级；真正的细粒度拦截在 check_command_safety 里做
# （因为 run_command 大部分用法安全，只有少数命令危险，不能一刀切禁掉）。
TOOL_PERMISSIONS = {
    "read_file": PermissionLevel.READ_ONLY,
    "write_file": PermissionLevel.WRITE_SAFE,
    "run_command": PermissionLevel.WRITE_DESTRUCTIVE,
}


# 危险命令模式：正则匹配到就拦截。覆盖「删除 / 格式化 / 关机 / 覆盖系统文件 / 丢失 git 未提交工作」等不可逆操作。
# 教学项目，宁可拦得严一点（宁可误拦让用户放行，也不要漏掉一个 rm -rf）。
_DANGEROUS_COMMAND_PATTERNS = [
    (r"\brm\s+-[rf]", "rm -r/-f 递归或强制删除"),
    (r"\brmdir\b", "rmdir 删目录"),
    (r"\bdel\s+/[sqf]", "Windows del /s /q 批量删除"),
    (r"\bformat\b", "format 格式化磁盘"),
    (r"\bmkfs\b", "mkfs 格式化文件系统"),
    (r"\bdd\s+if=", "dd 磁盘写入"),
    (r":\(\)\s*\{.*\};", "fork bomb"),
    (r"\bshutdown\b", "shutdown 关机"),
    (r"\breboot\b", "reboot 重启"),
    (r">\s*/dev/sd", "直写块设备"),
    (r"\bchmod\s+-r\s+777", "chmod -R 777 危险提权"),
    # ── 会丢失 git 未提交工作的命令（真实事故：AI 曾 reset 掉半天工作）──
    # 只拦「会丢工作」的危险形态，不误伤安全用法（如 git checkout <分支名> 切分支是安全的）。
    (r"\bgit\s+reset\s+--hard", "git reset --hard 丢弃未提交改动"),
    (r"\bgit\s+checkout\s+--\s+\.", "git checkout -- . 丢弃工作区改动"),
    (r"\bgit\s+checkout\s+\.(\s|$)", "git checkout . 丢弃工作区改动"),
    (r"\bgit\s+clean\s+-[a-z]*f", "git clean -f 删除未跟踪文件"),
    (r"\bgit\s+restore\s+--staged\s+--worktree", "git restore 覆盖工作区"),
    (r"\bgit\s+push\s+.*--force\b", "git push --force 覆盖远端历史"),
    (r"\bgit\s+push\s+.*-f\b", "git push -f 覆盖远端历史"),
]

# 禁止触碰的系统目录前缀（路径遍历 / 写系统目录防护）。
_FORBIDDEN_PATH_PREFIXES = [
    "/etc/", "/usr/", "/bin/", "/sbin/", "/sys/", "/boot/", "/dev/",
    "c:\\windows", "c:\\program files",
]


def check_command_safety(command: str) -> tuple[bool, str]:
    """检查一条 shell 命令是否安全。返回 (是否放行, 原因)。

    对照书 8.2 支柱二「用代码强制规则」。run_command 被标为最危险级别，
    但不能一刀切禁掉（大部分命令是安全的 ls/echo/cat）——所以在这里做模式匹配，
    只拦真正危险的。命中危险模式 → 拒绝执行（返回 False），否则放行。
    """
    lowered = command.lower()
    for pattern, desc in _DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return False, f"命中危险命令模式「{desc}」"
    return True, "安全"


def check_path_safety(path: str) -> tuple[bool, str]:
    """检查一个文件路径是否安全（路径遍历 + 系统目录防护）。返回 (是否放行, 原因)。

    对照书里 FileWriteParams 的 validate_path：禁 `..` 路径遍历、禁写系统目录。
    """
    if ".." in path:
        return False, "路径含 `..`，禁止路径遍历"
    lowered = path.lower()
    for prefix in _FORBIDDEN_PATH_PREFIXES:
        if lowered.startswith(prefix):
            return False, f"禁止触碰系统目录：{prefix}"
    return True, "安全"


def check_tool_call(name: str, tool_input: dict) -> tuple[bool, str]:
    """执行前的统一权限关卡：按工具类型做对应的安全检查。返回 (是否放行, 原因)。

    这是 harness 卡在「模型请求 → 真正执行」之间的那道关。agent 在真正 execute_tool
    之前先过这里；不放行就不执行，把拒绝原因当 tool_result 回喂给模型让它换做法。
    """
    if name == "run_command":
        return check_command_safety(tool_input.get("command", ""))
    if name == "write_file":
        return check_path_safety(tool_input.get("path", ""))
    # read_file 只读，天然安全；未知工具交给 execute_tool 去报「未知工具」。
    return True, "安全"


# ─────────────────────────────────────────────────────────────
# 支柱② 死循环检测
# ─────────────────────────────────────────────────────────────

class LoopDetector:
    """死循环检测：连续 N 次完全相同的 action 就判定鬼打墙（对照书 8.2 支柱三配套）。

    为什么需要：模型有时会卡在同一个失败上——反复调同一个命令、同样的参数，
    十几轮都不换思路，白烧 token。检测到就注入一条「你在重复，换个思路」的提示打断它。

    「一次 action」= (工具名, 参数的规范化字符串)。连续 max_same 次相同 → 触发。
    """

    def __init__(self, max_same: int = 3):
        self.max_same = max_same
        # 记录最近若干次 action 的指纹（工具名+参数）。
        self._recent: list[str] = []

    def _fingerprint(self, name: str, tool_input: dict) -> str:
        """把一次工具调用压成一个可比较的指纹字符串。

        参数按 key 排序后拼接，保证 {"a":1,"b":2} 和 {"b":2,"a":1} 指纹相同
        （字典顺序不该影响「是否是同一个 action」的判断）。
        """
        items = sorted(tool_input.items())
        return f"{name}({items})"

    def record(self, name: str, tool_input: dict) -> None:
        """记录一次单工具 action。只保留最近 max_same 个，够判断即可。

        注：agent 主循环用的是 record_round（整轮），这个单工具版保留供
        「一轮只有一个工具」的简单场景 / 单元测试直接调用。
        """
        fp = self._fingerprint(name, tool_input)
        self._recent.append(fp)
        if len(self._recent) > self.max_same:
            self._recent.pop(0)  # 丢掉最老的，滑动窗口

    def record_round(self, tool_use_blocks) -> None:
        """记录**一整轮**的所有工具调用为一个指纹（agent 主循环用这个）。

        为什么整轮而非只取第一个工具（修掉只看 [0] 的两个误判）：
          - 方向 A（漏报）：多工具乱序循环 [读A,读B] / [读B,读A]，只看 [0] 指纹在 A/B 间
            交替，判不出循环。整轮 + **排序**后两者指纹相同 → 能判出。
          - 方向 B（误报）：第一个工具恰好连续相同、但整轮在推进
            （[跑测试,读日志]→[跑测试,写报告]），只看 [0] 全是"跑测试"→ 误判。
            整轮参与 → 整轮不同则指纹不同 → 不误判。
        排序保证"同一组工具、顺序不同"算同一个 action（顺序不该影响是否循环的判断）。
        判据只看**请求**（工具名+参数），不含执行结果——结果每次不同（如含时间戳）
        不应妨碍"反复调同一命令"的循环判定。
        """
        fps = sorted(self._fingerprint(b.name, b.input) for b in tool_use_blocks)
        # 整轮压成一个字符串指纹，复用 is_looping 的「去重看长度」逻辑，零改动。
        self._recent.append(str(fps))
        if len(self._recent) > self.max_same:
            self._recent.pop(0)

    def is_looping(self) -> bool:
        """最近 max_same 次是否全部相同 → 判定死循环。"""
        if len(self._recent) < self.max_same:
            return False  # 还没攒够，不算
        return len(set(self._recent)) == 1  # 全相同 → set 里只剩 1 个

    def reset(self) -> None:
        """换了 action 或想清零时调用。"""
        self._recent.clear()


# ─────────────────────────────────────────────────────────────
# 支柱③ 验证门（自验证循环）
# ─────────────────────────────────────────────────────────────

# 检测「测试文件被删/大幅删减」的常见作弊模式（对照书 _check_test_deletions）。
# ⚠️ 注意：本函数**已实现且有单测，但未接入运行时**（agent.py 的验证门只跑检查命令、
#    不调本函数）。保留它是因为它是一个真实、独立可用的检测；是否接进 TAOR 循环留待日后。
def check_test_deletion(file_path: str, lines_added: int, lines_deleted: int) -> tuple[bool, str]:
    """检测一次写文件是否像「偷删测试来让测试过」。返回 (是否可疑, 原因)。

    作弊模式：改的是测试文件，且删的行数远多于加的行数（把测试内容掏空）。
    这是书里点名的「AI 常见作弊」——嘴上说测试过了，其实把测试删了。
    """
    if "test" not in file_path.lower():
        return False, "非测试文件"
    if lines_deleted > lines_added * 2 and lines_deleted > 5:
        return True, f"测试文件 {file_path} 删 {lines_deleted} 行仅加 {lines_added} 行，疑似掏空测试"
    return False, "正常"


class ValidationGate:
    """验证门：agent 声称「完成」前，强制过一道检查（对照书 8.2 支柱三，最高优先级）。

    书里说这是对成功率提升最大的单项（+7.1pp）——因为模型有「完成偏见」：
    倾向于宣布成功而不真正验证。验证门就是在它说「我做完了」时，反问一句
    「那你证明给我看」——跑一遍检查命令，过了才算真完成，没过就打回去继续修。

    本项目做「可选的检查命令」：如果任务方（调用者）给了检查命令（如 pytest），
    完成前就跑一遍；没给就跳过（不强加）。这样验证门对「需要验证的任务」生效，
    对「纯问答」不添乱。检查命令的实际执行由调用者注入的 runner 回调完成
    （同 context.py 的 summarizer 注入思路：纯逻辑可测，副作用隔离）。
    """

    def __init__(self, check_command: str | None = None):
        # 任务的完成判据命令（如 "py -m pytest -q"）。None = 不验证。
        self.check_command = check_command

    def verify(self, runner) -> tuple[bool, str]:
        """跑检查命令验证任务是否真完成。runner(command)->输出字符串。

        返回 (是否通过, 报告)。约定：检查输出里含 "FAIL"/"Error"/"错误"（不区分大小写）
        视为未通过。没配检查命令时直接放行（无条件通过）。
        """
        if not self.check_command:
            return True, "未配置检查命令，跳过验证"
        output = runner(self.check_command)
        low = output.lower()
        # 简单判据：输出里出现失败/错误关键词 → 未通过。教学项目够用。
        if "fail" in low or "error" in low or "错误" in low or "[未" in output:
            return False, f"验证未通过：\n{output}"
        return True, f"验证通过：\n{output}"
