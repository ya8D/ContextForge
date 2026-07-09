"""
tests/02_compaction_demo.py —— P3 真实场景演示（不是自动化测试，是跑给人看的）。

目的：用**真实的 500K 阈值**（不调小）亲眼看压缩在真实阈值处触发。
做法（贴合用户最初定的思路「让上下文真实涨到逼近 500K」）：
  - 造若干个各约 4.5 万字符的中等文件（每个刚好在回喂截断线 5 万之下，
    能被整个读进历史，不被 _truncate_for_feedback 砍掉）。
  - 让 agent **串行**逐个读它们（一次读一个、看到结果再读下一个），
    历史每轮 +~4.5 万字符（≈1.4 万 token），十几轮后累积逼近 500K → 真实触发压缩。

为什么必须串行：一轮并行读完的话，历史一次性涨完就结束了，中途没有「多轮累积」
可看，也切不出压缩的中段。串行才能看到「涨—涨—涨—到阈值—压」的完整过程。

⚠️ 会烧一些 token（读十几个大文件、多轮全量重发）。用户已确认不用省 token。
跑法：PYTHONUTF8=1 py tests/02_compaction_demo.py
"""

import os
import sys

# 让脚本能 import src/ 下的 contextforge 包（T3 起源码收进 src/contextforge/）。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from contextforge.agent import Agent  # noqa: E402
from contextforge.context import COMPACT_THRESHOLD_TOKENS  # noqa: E402

# 每个文件的字符数：4.5 万（<5 万截断线，保证整块进历史不被砍）。
FILE_CHARS = 45_000
# 造多少个文件：45000 字符 ≈ 1.4 万 token/文件；要逼近 500K，需 ~35 个。
# 但我们让阈值 500K 真实生效，多造几个确保能越过阈值触发压缩。
NUM_FILES = 40

# 大文件放在一个临时子目录里，跑完可删。
DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_compaction_demo_files")


def _make_big_files() -> list[str]:
    """造一批中等大小的文本文件，每个内容不同（避免完全相同被缓存/去重）。"""
    os.makedirs(DEMO_DIR, exist_ok=True)
    paths = []
    for i in range(NUM_FILES):
        path = os.path.join(DEMO_DIR, f"doc_{i:02d}.txt")
        # 每个文件内容带自己的编号，且塞满到 FILE_CHARS。内容是可读的中文段落重复，
        # 让摘要模型能总结出「这些是编号 doc_XX 的文档」。
        header = f"===== 文档编号 doc_{i:02d} =====\n本文件是压缩演示用的第 {i} 号文档。\n"
        filler = f"[doc_{i:02d} 第{{}}段] 这是一段用于把上下文撑大的占位文本。".format
        body_lines = []
        n = 0
        cur = len(header)
        while cur < FILE_CHARS:
            line = filler(n) + "\n"
            body_lines.append(line)
            cur += len(line)
            n += 1
        content = header + "".join(body_lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        paths.append(path)
    return paths


def main():
    # 支持缩小预演：传 "--small" 先用少量文件 + 小阈值验证脚本逻辑，
    # 确认端到端跑通、压缩在预期处触发，再放心跑真实 500K 满配（不带参数）。
    small = "--small" in sys.argv
    global NUM_FILES
    if small:
        NUM_FILES = 6
        threshold = 50_000  # 小阈值：读 3-4 个文件就越过，快速验证链路
        print("【缩小预演模式】6 个文件 + 阈值 5 万 token，快速验证脚本逻辑。")
    else:
        threshold = COMPACT_THRESHOLD_TOKENS

    print("=" * 60)
    print(f"P3 真实压缩演示：阈值 = {threshold:,} token")
    print(f"造 {NUM_FILES} 个文件，每个约 {FILE_CHARS:,} 字符，串行逐个读，")
    print("看上下文一轮轮涨、逼近阈值时真实触发压缩、任务不断线。")
    print("=" * 60)

    paths = _make_big_files()
    print(f"✅ 已造 {len(paths)} 个文件于 {DEMO_DIR}")

    # max_iterations 给足，串行读 N 个文件要 N+ 轮。
    agent = Agent(max_iterations=NUM_FILES + 20, compact_threshold=threshold)

    # 任务：强制串行逐个读，读完报告每个文件的编号，最后统计。
    # 用相对路径列出文件名，让模型一个一个 read_file。
    file_list = "、".join(f"doc_{i:02d}.txt" for i in range(NUM_FILES))
    task = (
        f"下面这些文件都在目录 {DEMO_DIR} 里，请**严格一次只读一个**、"
        f"看到内容后再读下一个（绝对不要在同一轮里并行读多个）：{file_list}。"
        f"每读完一个，只需简短报告它的文档编号（如 doc_00）。"
        f"全部读完后，告诉我你一共读了几个文档、它们的编号范围。"
    )

    final = agent.run(task)

    print("\n" + "=" * 60)
    print("最终答案：", final)
    print("=" * 60)
    print("\n👀 回看上面的日志，找 🗜️ [压缩] 行：")
    print("   - 上下文规模在哪一轮越过 500,000 token；")
    print("   - 压缩把消息压掉几轮、保留最近几轮；")
    print("   - 压缩后 cache_read 是否回落、cache_write 是否上涨（缓存链断一次）；")
    print("   - 最终答案是否仍完整（任务没被压断）。")
    print(f"\n临时文件在 {DEMO_DIR}，看完可手动删。")


if __name__ == "__main__":
    main()
