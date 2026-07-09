"""
cli.py —— ContextForge 的正式 CLI 入口（原 main.py）。

用法（`pip install -e .` 后可在任意目录直接敲 `contextforge`，或用短别名 `cf`）：
    contextforge      # 或 cf

启动后进入交互循环：输入任务 → agent 跑 TAOR → 打印结果 → 继续。
输入 exit / quit / q 退出。

说明：
- 同一个 Agent 实例贯穿整个会话，self.messages 会累积 —— 所以后一个任务
  能记得前一个（这就是「短期记忆」的雏形）。想清空记忆重新开始，输入 reset。
- 每轮的完整 in/out 会落盘到 <项目根>/traces/<年>/<月>/<日>/run_<时分秒>/，供调查 KV Cache。
"""

import sys

from contextforge.agent import Agent
from contextforge.harness import ValidationGate
from contextforge.tools import TOOL_SCHEMAS


def main() -> int:
    agent = Agent()
    print("=" * 56)
    print("  ContextForge —— 输入任务开始；exit/quit 退出；reset 清空记忆")
    print("  /compact [要求] —— 手动压缩历史（可跟一段话指定保留/删除什么）")
    print("  /check [命令] —— 设验证门检查命令（如 /check py -m pytest -q）；空=查看，off=清除")
    print(f"  模型：{agent.model}")
    print(f"  可用工具（发给模型的菜单，共 {len(TOOL_SCHEMAS)} 个）：")
    for t in TOOL_SCHEMAS:
        print(f"    · {t['name']}：{t['description']}")
    print("=" * 56)

    while True:
        try:
            task = input("\n你的任务> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0

        if not task:
            continue
        if task.lower() in {"exit", "quit", "q"}:
            print("再见。")
            return 0
        if task.lower() == "reset":
            agent = Agent()
            print("（已清空记忆，开启新会话）")
            continue
        # T5-A 主动压缩：/compact [要求]。空要求 = 无特别要求（回退会话级偏好/默认四维）。
        if task.lower() == "/compact" or task.lower().startswith("/compact "):
            directive = task[len("/compact"):].strip() or None
            print(agent.compact_now(directive=directive))
            continue
        # 验证门检查命令：/check [命令] 设本会话；空=查看当前；off/none/clear=清除。
        # 命令存在 Agent 实例上，reset 重建实例即清空、回到环境变量默认。
        if task.lower() == "/check" or task.lower().startswith("/check "):
            arg = task[len("/check"):].strip()
            if not arg:
                cur = agent.check_command
                print(f"当前验证门命令：{cur}" if cur else "未配置验证门命令，声称完成时直接放行（跳过验证）。")
            elif arg.lower() in {"off", "none", "clear"}:
                agent.check_command = None
                agent.validation_gate = ValidationGate(check_command=None)
                print("已清除验证门命令，之后声称完成直接放行。")
            else:
                agent.check_command = arg
                agent.validation_gate = ValidationGate(check_command=arg)
                print(f"已设验证门命令：{arg}（下个任务声称完成时会强制跑一遍，失败打回）")
            continue

        final = agent.run(task)
        print("\n" + "=" * 56)
        print("最终答案：", final)


if __name__ == "__main__":
    sys.exit(main())
