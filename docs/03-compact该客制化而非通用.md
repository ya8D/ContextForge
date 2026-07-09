# 03 · compact 该客制化，而非通用

## 问题是什么

[02](./02-上下文压缩不是截断API而是本地重写历史.md) 说清了压缩是本地重写历史。那**摘要该怎么写**？
最直接的做法：写一个通用的「把这段历史总结一下」的 prompt，一劳永逸。但通用压缩有个隐藏的问题——
它**不知道你此刻在干嘛**，只会机械地中立总结，什么都留一点、什么都不突出。通用即中立，中立即「哑」。

## 我原来怎么想

**误区一**：以为通用压缩够用了。总结嘛，把长的变短，还能怎样？

**误区二**（更微妙，我真讲错过）：当我想「让压缩更聪明」时，第一反应是「派个子 agent 去压」，
理由是——"当前这个 AI 的上下文已经被污染了，得找个干净的子 agent 来代为压缩、自救"。

这个理由**听起来很有道理，其实完全站不住**。

## 真相 / 原理

### 通用不如客制化：给压缩注入「当前目的」

通用压缩的问题是它不知道「你要什么」。解法是让用户能给压缩下一段自然语言指令（directive），
告诉它**保什么、删什么**。实现上就是把 directive 叠加进摘要 prompt：

```python
# context.py：_build_summary_prompt
def _build_summary_prompt(directive: str | None) -> str:
    if not directive:
        return _SUMMARY_PROMPT   # 无 directive → 逐字等于默认四维（向后兼容）
    return (
        f"⚠️ 本次压缩有用户指定的特别要求，请**优先遵守**：{directive}\n"
        f"在满足上述要求的前提下，仍需保留下面的基础信息。\n\n"
        + _SUMMARY_PROMPT
    )
```

注意是**叠加而非替换**：四维基础要求（任务目标 / 已做 / 发现 / 下一步）是底线，用户的 directive
在其上加码（"重点保留登录相关报错，其余狠删"）。为什么不让 directive 直接替换？因为怕用户一句话
就把"保留任务目标"这种底线也冲掉了。客制化是**在中立底线之上叠偏好**，不是把底线也交出去。

### 子 agent 的真正价值：不是「代为自救」，是「配上工具和多轮判断」

回到我讲错的那个类比。`_summarize` 本来就是**一次干净、无历史、无工具的独立 LLM 调用**：

```python
# agent.py：_summarize（盲总结）—— 单独调一次，只带这段 prompt，不带主对话历史
def _summarize(self, prompt: str) -> str:
    resp = self.client.messages.create(
        model=self.model, max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],  # 干净的一次性调用
    )
    return "".join(b.text for b in resp.content if b.type == "text")
```

它**根本没有「被污染的上下文」**——它只看到你递给它的那段要压的文本。所以"当事人被污染、需要子
agent 代为自救"这个说法从头到尾就不成立。

那把执行者换成子 agent（`compact_executor="subagent"`）到底增量在哪？在于**给压缩这一步配上工具和
多轮判断力**：单轮 `_summarize` 只能盲总结一次、无法验证；子 agent 能 `read_file` 回读原始文件
**核实某个结论是否还成立**、能多轮推敲该留什么。

这一点**实测坐实了**：构造 `Agent(compact_executor="subagent")`，历史里故意把一个探针文件的值
说成过期的 `STALE`，真实文件里写的是 `CURRENT`。directive 让压缩时回读核实。结果子 agent **真的
启动了自己的 TAOR 循环、真的 `read_file` 回读**，摘要里写出了文件真实值 `CURRENT`——而盲总结只有
历史里的 `STALE` 可抄，绝不可能写出 `CURRENT`。`CURRENT` 一旦出现，就是子 agent 真核实过的铁证。
（见 [tests/test_e2e.py](../tests/test_e2e.py) 的 `test_subagent_executor_reverifies_by_reading_file`。）

## 学到的道理

- **通用压缩是「哑」的**：它不知道当前目的，只能中立总结。客制化 = 把「你此刻要什么」注入进去，
  让压缩朝目的裁剪（狠删噪声、重点留相关）。
- **给一个机制配"更强的执行者"时，先想清楚增量到底是什么**。我差点用一个错误的理由（代为自救）
  去正当化子 agent，其实真正的增量是**工具 + 多轮验证能力**——能核实、能推敲，而非盲总结。
  理由错了，即使结论（用子 agent）碰巧对，也说明我没真懂。
- **"听起来有道理"是最危险的信号**。"当事人被污染，找个干净的来自救"——这话顺口、像模像样，
  但一对照 `_summarize` 本来就是干净独立调用，立刻崩塌。写下理由、拿代码验一遍，比感觉靠谱。
- 延伸阅读：客制化压缩也有边界——用户给的 directive 措辞太强硬（"全部删除、绝不出现"）会触发
  模型的安全拒答，见 [04](./04-compact指令本身也要过模型安全判断.md)。
