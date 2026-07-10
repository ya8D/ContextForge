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

import os
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
    (r"\brm\s+--(recursive|force)", "rm --recursive/--force 长选项删除"),  # 审查 #7：长选项绕过
    (r"\brmdir\b", "rmdir 删目录"),
    (r"\bdel\s+/[sqf]", "Windows del /s /q 批量删除"),
    (r"\bdel\s+.*\*", "Windows del 通配删除"),          # 审查 #7：裸 del *.py 绕过
    (r"\berase\s", "Windows erase 删除"),
    (r"\bmove\s", "move 移动（可覆盖/搬走数据）"),        # 审查 #7
    (r"\bformat\b", "format 格式化磁盘"),
    (r"\bmkfs\b", "mkfs 格式化文件系统"),
    (r"\bdd\b.*\bof=", "dd 磁盘写入（of= 目标，含 nvme/hd 等）"),  # 审查 #7：dd of=... 顺序/设备族
    (r":\(\)\s*\{.*\};", "fork bomb"),
    (r"\bshutdown\b", "shutdown 关机"),
    (r"\breboot\b", "reboot 重启"),
    (r">\s*/dev/(sd|nvme|hd|vd|mmcblk)", "直写块设备"),   # 审查 #7：补 nvme/hd/vd/mmcblk
    (r"\bchmod\s+-r\s+777", "chmod -R 777 危险提权"),
    (r"\bremove-item\b.*(-recurse|-force)", "PowerShell Remove-Item -Recurse/-Force"),  # 审查 #7
    (r"\bfind\b.*-delete", "find -delete 递归删除"),      # 审查 #7
    (r"\bfind\b.*-exec\s+rm", "find -exec rm 递归删除"),  # 审查 #7
    (r"\btype\s+nul\s*>", "type nul > 截断文件"),          # 审查 #7：截空文件
    (r"\bxargs\s+rm\b", "xargs rm 删除"),                 # 审查 #7
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

# cmd.exe / shell 混淆元字符：出现即拒（审查 #7）。
# 黑名单正则扫的是**展开前**的字符串，`r^m`（^ 是 cmd 转义、运行时被去）、`%A%`（变量展开）、
# 反引号 / $() 命令替换都能让危险 token 在扫描时「隐身」、运行时才现形。无法用正则看穿展开，
# 故直接**拒绝含这些混淆字符的命令**——正常命令用不到它们，代价可接受（教学项目宁严勿漏）。
_OBFUSCATION_CHARS = [
    ("^", "cmd.exe 转义符 ^（可拆散危险 token）"),
    ("%", "cmd.exe 变量展开 %VAR%（可隐藏危险 token）"),
    ("`", "反引号命令替换"),
    ("$(", "$() 命令替换"),
]

# 禁止触碰的系统目录前缀（路径遍历 / 写系统目录防护）。
# ⚠️ 审查 #6：这里的判定必须配合 check_path_safety 里的**归一化**（abspath+normcase），否则
# 正斜杠 `C:/Windows`、驱动器相对 `c:windows\x`、CWD 相对 `system32\x`、其它盘符都能绕过。
# 前缀本身用小写反斜杠形态（Windows）/ 小写正斜杠（Unix），与 normcase 后的路径对齐。
_FORBIDDEN_PATH_PREFIXES = [
    # Unix 系统目录
    "/etc/", "/usr/", "/bin/", "/sbin/", "/sys/", "/boot/", "/dev/",
    "/root/", "/var/", "/lib/",
    # Windows 系统目录（normcase 会把盘符与分隔符归一成小写反斜杠）
    "c:\\windows", "c:\\program files", "c:\\programdata",
]


def _normalize_path(path: str) -> str:
    """把路径归一化成可靠比较的形态：转绝对路径 + 统一分隔符/大小写（审查 #6）。

    os.path.abspath 会：① 把相对/驱动器相对路径按 CWD 展开成绝对路径（堵住 `system32\\x`
    这种 CWD 相对绕过）；② 把正斜杠统一成平台分隔符（Windows 上 `C:/Windows` → `C:\\Windows`）。
    os.path.normcase 再转小写（Windows 大小写不敏感），与小写前缀对齐。
    注：abspath 不解析符号链接/junction（那需要 realpath、且要求路径真实存在）；对本教学项目，
    堵住正斜杠/驱动器相对/CWD 相对/大小写这些**无需落盘即可判定**的绕过已是主要收益。
    """
    return os.path.normcase(os.path.abspath(path))


def check_command_safety(command: str) -> tuple[bool, str]:
    """检查一条 shell 命令是否安全。返回 (是否放行, 原因)。

    对照书 8.2 支柱二「用代码强制规则」。run_command 被标为最危险级别，
    但不能一刀切禁掉（大部分命令是安全的 ls/echo/cat）——所以在这里做模式匹配，
    只拦真正危险的。命中危险模式 → 拒绝执行（返回 False），否则放行。
    """
    lowered = command.lower()
    # 先拒混淆元字符（^ / %VAR% / 反引号 / $()）——它们能让危险 token 在扫描时隐身、运行时才现形，
    # 正则无法看穿展开（审查 #7）。正常命令用不到，宁严勿漏。
    for ch, desc in _OBFUSCATION_CHARS:
        if ch in command:
            return False, f"命中混淆字符「{desc}」"
    for pattern, desc in _DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return False, f"命中危险命令模式「{desc}」"
    return True, "安全"


def check_path_safety(path: str) -> tuple[bool, str]:
    """检查一个文件路径是否安全（路径遍历 + 系统目录防护）。返回 (是否放行, 原因)。

    对照书里 FileWriteParams 的 validate_path：禁 `..` 路径遍历、禁写系统目录。
    审查 #6 修正：比较前先 _normalize_path（abspath+normcase）——否则正斜杠 `C:/Windows`、
    驱动器相对 `c:windows\\x`、CWD 相对 `system32\\x`、其它盘符都能绕过纯字符串前缀匹配。
    UNC 网络路径（\\\\host\\share）单独拦：不该往远程共享读写。
    """
    if ".." in path:
        return False, "路径含 `..`，禁止路径遍历"
    # UNC 网络路径（\\host\share 或 //host/share）：拦，防往远程共享读写（审查 #6）。
    if path.startswith("\\\\") or path.startswith("//"):
        return False, "禁止 UNC 网络路径（\\\\host\\share）"
    # 两种形态都比对，任一命中即拦（审查 #6）：
    #   ① abspath+normcase 后的绝对路径——堵 CWD 相对 / 驱动器相对 / 正斜杠 / 大小写绕过（主要修复）。
    #   ② 原始路径仅统一斜杠+小写（不 abspath）——保住「Unix 风格绝对路径 /etc/... 在任何平台都拦」
    #      的跨平台意图；否则 Windows 上 abspath 会把 /etc 展开成当前盘符 C:\etc、Unix 前缀永不命中。
    norm_abs = _normalize_path(path)
    norm_raw = path.replace("/", os.sep).lower()
    for prefix in _FORBIDDEN_PATH_PREFIXES:
        # Unix 前缀用正斜杠形态比原始路径；Windows 前缀已是反斜杠形态。两种归一化都试。
        prefix_raw = prefix.replace("\\", os.sep)
        unix_prefix = prefix.replace("\\", "/")
        if (norm_abs.startswith(prefix) or norm_raw.startswith(prefix_raw)
                or path.lower().startswith(unix_prefix)):
            return False, f"禁止触碰系统目录：{prefix}"
    return True, "安全"


def check_tool_call(name: str, tool_input: dict) -> tuple[bool, str]:
    """执行前的统一权限关卡：按工具类型做对应的安全检查。返回 (是否放行, 原因)。

    这是 harness 卡在「模型请求 → 真正执行」之间的那道关。agent 在真正 execute_tool
    之前先过这里；不放行就不执行，把拒绝原因当 tool_result 回喂给模型让它换做法。
    """
    if name == "run_command":
        return check_command_safety(tool_input.get("command", ""))
    if name in ("write_file", "read_file"):
        # 审查 #5：read_file 原先落兜底放行、完全不过路径检查——read_file("/etc/shadow") /
        # C:/Windows/System32/config/SAM / ../../../.env 会把敏感文件读入模型上下文。只读也能泄密，
        # 故与 write_file 一样过 check_path_safety（禁 `..`/系统目录/UNC）。
        return check_path_safety(tool_input.get("path", ""))
    # 未知工具交给 execute_tool 去报「未知工具」。
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

# 注：曾有过一个 check_test_deletion（检测「偷删测试骗绿」的作弊模式）。它一度被接进
# write_file，但那是错误的接入点——write_file 是覆盖写、只有新旧行数、拿不到真实 diff，
# 用「净行数差」近似判掏空会**两头都错**：合法精简测试（20→8 行）被误拦（假阳性），
# 而真掏空（40 行断言全换成 40 行 pass，行数不变）漏过（假阴性，恰恰是最典型的作弊手法）。
# 要可靠区分「掏空」vs「合法精简」需语义级信息（AST 层面数断言/空函数体），对本教学项目过重。
# 故已彻底移除该函数及其接入。「删空测试骗绿」作为已知未处理问题记录在 PROGRESS.md。


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

    def __init__(self, check_command: str | None = None, timeout: int = 300):
        # 任务的完成判据命令（如 "py -m pytest -q"）。None = 不验证。
        self.check_command = check_command
        # 验证门跑检查命令的超时（秒）。审查 #4：测试套件常 >30s，给它一个宽松默认（5 分钟），
        # 与模型自己调 run_command 的 30s 分开——那是防挂死，这是等测试跑完。
        self.timeout = timeout

    def verify(self, runner) -> tuple[bool, str]:
        """跑检查命令验证任务是否真完成。

        runner(command, timeout) -> (退出码, 输出字符串)。退出码是命令成败的**唯一可靠判据**
        （审查 #3）：0=通过，非 0=失败，None=没跑起来（超时/异常）视为未通过。
        文本关键词判据仅在**拿不到退出码**时（None）作兜底提示，不再作为主判据——原先裸子串
        匹配 fail/error 会把含 `test_error_x`/`0 errors` 的合格输出误判失败、把无关键词的真失败
        误判通过（语义两个方向都会错）。

        返回 (是否通过, 报告)。没配检查命令时直接放行（无条件通过）。
        """
        if not self.check_command:
            return True, "未配置检查命令，跳过验证"
        exit_code, output = runner(self.check_command, self.timeout)
        if exit_code == 0:
            return True, f"验证通过（退出码 0）：\n{output}"
        if exit_code is None:
            # 命令没跑起来（超时/异常）——不是「测试失败」，是「没跑完」，如实说明并打回。
            return False, f"验证未完成（命令未正常结束，如超时/异常）：\n{output}"
        return False, f"验证未通过（退出码 {exit_code}）：\n{output}"
