"""
tests/01_run_agent.py —— Phase 1 手动演示脚本（不是自动化测试）。
给 agent 一个需要多步工具的任务，观察完整 TAOR 轨迹（靠肉眼看，无断言）。

注意：这是「跑给人看轨迹」的演示脚本，不会被 pytest 收集（文件名不匹配 test_*.py）。
真正带断言的自动化测试见 tests/test_tools.py / test_agent_logic.py / test_e2e.py。
"""

import os
import sys

# 让脚本能 import src/ 下的 contextforge 包（T3 起源码收进 src/contextforge/）。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from contextforge.agent import Agent  # noqa: E402

if __name__ == "__main__":
    agent = Agent()
    # 这个任务需要两步：先列目录看有哪些文件，再读其中一个文件并总结。
    task = (
        "先用 run_command 列出 C:/AI_learning/myagent 目录下有哪些文件，"
        "然后用 read_file 读取其中的 requirements.txt，"
        "最后用一句话告诉我这个项目依赖了哪些库、分别做什么。"
    )
    final = agent.run(task)
    print("\n" + "=" * 50)
    print("最终答案：", final)
