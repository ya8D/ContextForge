"""
cli.py —— myagent 的正式 CLI 入口（原 main.py，T3 标准包布局后改名）。

用法（T2 完成 `pip install -e .` 后可直接敲 `myagent`；目前过渡期用）：
    cd src && py -m myagent.cli

启动后进入交互循环：输入任务 → agent 跑 TAOR → 打印结果 → 继续。
输入 exit / quit / q 退出。

说明：
- 同一个 Agent 实例贯穿整个会话，self.messages 会累积 —— 所以后一个任务
  能记得前一个（这就是「短期记忆」的雏形）。想清空记忆重新开始，输入 reset。
- 每轮的完整 in/out 会落盘到 myagent/traces/run_<时间戳>/，供调查 KV Cache。
"""

import sys

from myagent.agent import Agent
from myagent.tools import TOOL_SCHEMAS


def main() -> int:
    agent = Agent()
    print("=" * 56)
    print("  myagent CLI —— 输入任务开始；exit/quit 退出；reset 清空记忆")
    print("  /compact [要求] —— 手动压缩历史（可跟一段话指定保留/删除什么）")
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

        final = agent.run(task)
        print("\n" + "=" * 56)
        print("最终答案：", final)


if __name__ == "__main__":
    sys.exit(main())
