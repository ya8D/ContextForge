# 01 · KV Cache：每轮全量历史为何不贵

## 问题是什么

TAOR 循环有个看着很吓人的设计：**每一轮都把完整历史重新发给模型**。API 是无状态的，它不记得
上一轮，所以第 5 轮要把第 1~4 轮的所有对话原样再发一遍，第 20 轮要发前 19 轮……

直觉上这是灾难：历史线性增长，重发的量也线性增长，token 岂不是平方级地烧？那超大项目（Chromium）
动辄几十万 token 的上下文，每轮全量重发，钱包不得原地爆炸？

## 我原来怎么想

我以为「发出去多少 token = 账单按多少 token 算」。于是第 5 轮发了 3 万 token，就该按 3 万收费；
下一轮历史更长，按 4 万收费。全量重发 = 每轮都为整段历史付全价。

按这个模型，压缩就成了**唯一的救命稻草**——不压，成本就随轮数失控。

## 真相 / 原理

亲手看 trace 里的 `usage` 才发现，我把「发出去的量」和「按全价算的量」搞混了。一轮真实的 usage 长这样：

```json
"usage": {
  "input_tokens": 2,
  "cache_read_input_tokens": 1105,
  "cache_creation_input_tokens": 154,
  "output_tokens": 512
}
```

`input_tokens = 2`——第 5 轮明明发了上千 token 的历史，为什么 input 只有 2？

因为 **input_tokens 只是「这一轮新增的、没被缓存命中的部分」**，不是发出去的总量。真正发出去的总量是三者相加：

```python
# context.py：current_context_tokens
def current_context_tokens(usage: dict) -> int:
    return (
        (usage.get("input_tokens") or 0)               # 未缓存的新增量（全价）
        + (usage.get("cache_read_input_tokens") or 0)   # 命中缓存的部分（~0.1x）
        + (usage.get("cache_creation_input_tokens") or 0)  # 首次写缓存的部分
    )
```

**KV Cache 的魔法**：前 4 轮的历史，模型上一轮已经算过、缓存了。这一轮重发时，那 1105 token 直接
命中 `cache_read`，只按**约 1/10 的价**计费；真正全价的只有本轮新冒出来的 2 个 token（`input_tokens`）。

所以「全量重发」在**流量**上是线性增长，但在**成本**上远没那么可怕——绝大部分历史命中缓存、廉价重放。
`input + cache_read + cache_write` 才是真实发出总量，其中只有 `input` 那一小截是全价。

> 术语对照：`ContextForge` 里 `in=..` 那行 log 打印的就是拆开的三段——
> `in=2 (cache_read=1105, cache_write=154)`，一眼看出「本轮真正全价的只有 2」。

## 学到的道理

- **「发出去的量」≠「按全价算的量」**。看账单要看 usage 的三个字段怎么分布，不能只看总流量吓自己。
- **KV Cache 让「无状态 API + 每轮全量重发」这个看似浪费的设计变得可行**。这也是为什么主流 agent
  都敢这么做——不是它们不在乎钱，是缓存把重发的边际成本压下去了。
- **压缩仍然有用，但它救的主要是「质量」不是「成本」**：历史太长，模型对中段的召回会下降
  （lost in the middle），而且缓存也不是无限的。压缩把中段变短，是为了让模型看得清、也顺带省下
  那部分持续开销——但别把压缩当成「不压就破产」的唯一防线，缓存已经扛住了大头。
- **凡是「看起来很浪费」的设计，先去量一下真实数据再下结论**。我差点因为一个错误的成本模型，
  把压缩的重要性判断偏了。trace 里的 `usage` 是 API 亲口告诉你的真相，比任何直觉都准。
