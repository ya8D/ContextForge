---
name: work-process
description: The full closed-loop workflow for completing one TODO.md backlog item in the ContextForge (myagent) project — read the task, analyze, establish a factual baseline (reproduce the bug with a real example, or record current behavior for an improvement), implement, verify with that same example, harden it into a test that fails on the pre-fix baseline, run the regression suite, and finish with a PROGRESS.md changelog entry plus commit. Use this whenever the user asks to "do a TODO item", "fix P1/P2/...", "work through the backlog", "start the next task", "修一个 todo", "做 backlog 里的 X", or otherwise wants a backlog task taken from start to a verified, tested, logged finish. Also use it when the user describes a bug or improvement in this repo and wants it carried all the way through — not just a quick patch. Prefer this skill over an ad-hoc fix whenever the goal is a complete, verified, regression-safe change rather than a one-off edit.
---

# Work Process — 一个 TODO 任务的完整闭环

这个 skill 把 ContextForge（myagent）项目里「做完一条 backlog 任务」的整套工作法固化下来。
它的存在价值是：**保证每条任务都被真实验证过、有测试兜底、不弄坏别的、留下记录**——而不是
打个补丁就算完。过去多轮实践反复证明：跳过其中任何一步，都会留下「看起来修了、其实没验证」
或「测试恒真、根本测不出问题」的隐患。

## 核心原则（贯穿全程，比步骤更重要）

- **实测推翻臆测，能用真实 API 就用真实 API。** 这是本项目最硬的教训。假 client（mock
  `messages.create`）会骗人——它让你对异常类型、`max_tokens` 行为、pytest 输出等做出错误假设，
  然后你「验证通过」的其实是自己脑补的契约。token 不是约束；一次真实调用胜过十次假设。
  典型翻车：曾假设未配对 tool_use 抛 `BadRequestError`，真实是代理包成 `InternalServerError(500)`；
  曾假设 pytest 会打印测试文件名导致误判，真实 `-q` 模式根本不打印。**先真实复现，再动手。**
- **测试有效 ⟺ 破坏功能时它会 fail。** 一个在「功能被破坏」时仍 pass 的测试是无效的（恒真）。
  只断言 `isinstance(x, str)` 而 `x` 必为 str，就是典型恒真断言。固化测试时，必须确认它在
  **未修复的基线**上真的会 fail（否则它测不出任何东西）。
- **不是每条任务都是 bug。** bug 类任务有「失败反例」可跑；改进/增强类任务没有——别硬编一个假反例。
  对后者，改成「先记录改之前的基线行为」。第 3 步据此分叉。
- **收尾是任务的一部分，不是可选项。** TODO.md 明写：每个待办收尾 = 实现 + 测试跑绿 +
  PROGRESS.md 顶部追加变更日志。少一样都不算做完。

## 前置检查 + 八步闭环

### 0. 开工前置检查（每次开始新任务，最先做）
先同步 main 并确认上一个 PR 已合并，才开始新任务——否则新分支基于过时 main、还可能漏掉未合并的工作。
- `git checkout main && git pull --rebase`（把已合并的 PR 拉进本地 main）。
- 确认**上一个 PR 已合并**：`gh pr view <编号>` 看 `state: MERGED`，或确认 main 含它的 merge commit。
  **没合并就停下**，先请用户 review/合并，别在旧 main 上开新分支。
- 确认后，从最新 main 切 `feat/<任务>` 分支：`git checkout -b feat/<任务>`。

### 1. 读 TODO.md，选定任务，定性
读 [TODO.md](../../../TODO.md)，找到要做的那一条。先判断它是 **bug** 还是 **改进/增强**——这决定
第 3 步怎么走。把任务的「改什么、为什么」用一两句话说清楚（说不清就先问用户，别猜）。

### 2. 读相关源码 + 测试，分析现状
读任务点到的文件（源码 + 对应测试）。目标是精确定位「改哪几行、现状为什么是这样」。
优先用 Read/Grep 直接读，别凭记忆——代码可能已被前几轮改动过。

### 3. 建立「改之前」的事实基线
这一步分叉：
- **bug 类**：写一个**反例**证明当前确实有问题。**优先真实 API/真实子进程**（实测更稳）。
  跑它，亲眼看到 bug 发生（如「并行写同一文件 30 次，结果 29:1 不确定」）。记下这个反例。
- **改进/增强类**：没有失败反例，改成**记录现状的基线行为**——改之前是什么样、有什么局限
  （如「max_tokens=2048，一个大文件一轮写不完」）。这是后面对照「改好没」的锚点。

### 4. 实施修复 / 改进
按第 2 步的分析动手改。改动聚焦任务本身，不顺手重构无关代码。

### 5. 用第 3 步的同一个反例/基线复现验证
**用第 3 步那个反例**再跑一次，确认修复后行为正确。对 bug 类，这形成对照：
**同一个反例，修复前 fail、修复后 pass**——这就是「实测」的力量，也是最强的修复证据。

### 6. 把反例固化为正式 test（关键：验证它在基线会 fail）
把第 3 步的反例写成 `tests/` 里的正式测试。**然后做一件容易被漏的事**：确认这个 test 在
**未修复的基线**上真的会 fail。做法：`git stash` 把修复暂存起来 → 跑该 test 看它 fail →
`git stash pop` 恢复修复 → 再跑看它 pass。若基线上它不 fail，说明它是恒真的假测试，重写。
（真实 API 类反例适合放 `tests/test_*_e2e.py` 并标 `@pytest.mark.e2e`；纯逻辑放对应
`test_*.py`。参考 [tests/test_audit_fixes_e2e.py](../../../tests/test_audit_fixes_e2e.py) 这个
「基线全 fail、修复后全 pass」的范例。）

### 7. 跑全量回归，确认没弄坏别的
跑 `py -m pytest -m "not e2e"`（纯逻辑、毫秒级、不烧钱）确认全绿。改动碰了会真调 API 的路径时，
再挑相关 e2e 单独跑一遍。全绿才算「没引入回归」。

### 8. 收尾：PROGRESS.md 日志 + feature 分支 + PR
- 在 [PROGRESS.md](../../../PROGRESS.md) **顶部**追加一条变更日志：改了什么、为什么、怎么验证的、
  测试结果。时间倒序（最新在最上）。
- **在 `feat/<任务>` 分支上开发**（如 `feat/p3-compact-keep-user-msgs`），不在 main 上直接改。
  若一开始就在 main 上动了手，收尾时 `git checkout -b feat/<任务>` 把改动挪到新分支。
- commit（AI 做，本地可逆），commit message 说清 why（不只是 what），按项目现有风格。
- 推 **feature 分支** + `gh pr create` 开 PR，PR body 说清做了什么、怎么验证的。
- **绝不直接碰 main**：不 push main、不合并 PR。main 的更新只能由用户在 GitHub 上 review 后合并
  ——这是用户练 review、把关合并的关卡。AI 只负责把改动做好、推分支、开 PR，到此为止。

## 常见反模式（踩过的坑，别再犯）
- **跳过第 3 步直接改**：没有「改之前」的事实，就无法证明「改之后」真的更好。
- **固化了恒真测试**：第 6 步不验证「基线会 fail」，就可能留下一个永远 pass、测不出退化的假测试。
- **用假 client 代替真实 API 后就下结论**：假 client 的行为是你脑补的，真实行为常常不同。
- **改完不跑全量回归**：只测了改的那块，没发现顺手弄坏了别处。
- **忘记 PROGRESS.md 日志**：违反项目铁律（每笔改动必追 CHANGELOG）。
- **直接在 main 上开发 / 推 main / 自己合并 PR**：越权。改动走 `feat/<任务>` 分支 + PR，main 只能由
  用户在 GitHub review 后合并。AI 到「推分支 + 开 PR」为止。

## 运行环境备忘（本项目特定）
- Windows，用 `py` 启动器（bash 里 `python`/`python3` 可能不通）。中文输出加 `PYTHONUTF8=1` 防乱码。
- 纯逻辑测试：`py -m pytest -m "not e2e"`；真调 API 的：`py -m pytest -m e2e`。
- 稳定约定见 [CLAUDE.md](../../../CLAUDE.md)（语言用中文、模型 ID 从环境读、不引框架等）。
