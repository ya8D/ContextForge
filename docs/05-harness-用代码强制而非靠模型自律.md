# 05 · harness：用代码强制，而非靠模型自律

## 问题是什么

给 agent 配了工具（跑命令、写文件），它就有了**动手改变世界**的能力。可模型不是永远靠谱——它会
基于"想象的文件内容"盲目覆盖、会跑出 `rm -rf` 这种危险命令、会嘴上说"任务完成了"其实根本没验证。
怎么防？

## 我原来怎么想

第一反应是**在 prompt 里叮嘱**——"写文件前请先读一遍"、"不要跑危险命令"、"完成前请自己验证"。
本质是**指望模型自觉遵守规则**。

这条路有个致命问题：**模型的"遵守"是概率性的**。你叮嘱十次，它九次听话，第十次照样盲改、照样
说假话。而 agent 是要真的动手的——第十次的代价可能是覆盖了你的代码、删了你的文件。**靠自律防不住
需要 100% 拦住的事**。

## 真相 / 原理

正确的姿势：**约束不写在 prompt 里，而是用代码卡在「模型请求 → 真正执行」这道关口之间**。模型
可以请求任何操作，但请求和执行之间有一层 harness，不合规的请求根本到不了执行。这叫「用代码强制」。

ContextForge 的 harness 有几根柱子，都是这个模式：

### ① 先读再改：没读过的文件禁止写

模型想 `write_file` 一个已存在的文件？先检查它读过没有——没读过就直接拒绝，请求到不了写盘：

```python
# tools.py：write_file
if os.path.exists(path) and norm not in READ_FILES:
    return (f"[拒绝] 文件已存在但你还没读过它：{path}。"
            f"请先用 read_file 读取，确认当前内容后再写，避免盲目覆盖。")
```

这不是叮嘱模型"记得先读"，而是**物理上让"没读就写"这个动作失败**。模型再怎么想盲改，代码这关过不去。

### ② 危险命令运行时拦截

不预筛工具（那样太粗），而是在**执行前**用正则拦危险命令：

```python
# harness.py：check_command_safety（节选）—— rm -rf / format / mkfs / chmod -R 777 …
for pattern, reason in _DANGEROUS_COMMAND_PATTERNS:
    if re.search(pattern, command):
        return False, f"[拦截] 检测到危险命令（{reason}）：{command}"
```

后来还专门补了**危险 git 命令**（真实事故驱动：AI 曾把 git 整个 `reset --hard`、丢了半天工作）——
拦 `git reset --hard` / `git checkout -- .` / `git clean -f` / `git push --force`，但**不误伤**安全
用法（切分支、unstage、dry-run 照常放行）。关键是：**模型碰巧自己拒了不算数，harness 那道关照挡**。

### ③ 验证门：声称完成前强制验证

模型说"我做完了"就放它走？不行——若任务配了检查命令，**声称完成前强制跑一遍**，过了才算真完成：

```python
# agent.py：主循环里，模型不再要工具时
passed, report = self.validation_gate.verify(self._run_check)
if passed:
    return final                       # 通过 → 才放行
# 没过 → 把失败报告当 user 消息打回去，让模型继续修，不放行
self.messages.append({"role": "user", "content": f"[验证门] 你声称完成了，但强制检查未通过。…\n{report}"})
continue
```

这对治的是模型的**「完成偏见」**——倾向宣布成功而不真验证。验证门在它说"做完了"时反问一句
"证明给我看"。真实 e2e 里甚至观察到：模型不但被打回，还会**拒绝为通过验证门而伪造产物**
（它把"凭空建个文件骗过检查"判为作弊）——这正是"不要蒙混"提示的正面效果。
（见 [tests/test_e2e.py](../tests/test_e2e.py) 的 `test_validation_gate_rejects_false_completion`。）

### 副产品：约束逻辑可零成本单测

因为约束是代码、且**副作用靠回调注入**（验证门的 `runner`、压缩的 `summarizer` 都是传进来的），
纯判断逻辑能被单测全覆盖、不烧钱：

```python
# 测试验证门：传个假 runner，不真跑命令，只测「输出含 fail → 判未过」的决策
gate = ValidationGate(check_command="pytest")
passed, _ = gate.verify(lambda cmd: "FAILED test_x")   # 假回调
assert passed is False
```

## 学到的道理

- **能靠代码强制的，就别靠模型自律**。prompt 里的叮嘱是"软约束"（概率遵守），代码卡在执行前是
  "硬约束"（100% 拦截）。凡是"绝不能发生"的事，必须硬约束。
- **约束的位置很关键：卡在「请求 → 执行」之间**。模型可以自由请求，但请求到执行有一道代码闸门。
  这样既不限制模型的思考，又兜住了它的手。
- **"模型碰巧没犯错"不等于"防住了"**。危险 git 命令那条，模型有时自己会拒——但你不能依赖它这次
  刚好拒。harness 是那道无论模型怎样都在的关。
- **副作用注入为回调 = 逻辑与副作用分离 = 可测**。这不只是测试技巧，它逼你把"决策"和"动手"分开，
  代码本身也更清爽。详见各处的 `runner`/`summarizer` 回调设计。
- 具体的柱子还有各自的坑，比如死循环检测命中后**不该 reset**，见
  [06](./06-死循环检测-reset反而放过真循环.md)。
