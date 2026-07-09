# Backlog —— ContextForge 待办

> 本文件 = **未做的**。已完成的变更明细见 [PROGRESS.md](./PROGRESS.md)（开发日志），
> 稳定约定见 [CLAUDE.md](./CLAUDE.md)，学习笔记见 [docs/](./docs/)。

## 计划中

- **（可选）结构化输出 & 跨会话 Memory** —— 原规划的 P6。两块可独立做：
  - 结构化输出：让 agent 能按给定 schema 产出结构化结果（而非只回自然语言）。
  - 跨会话 Memory：把「短期记忆」（当前 `self.messages`，reset 即失忆）升级为可跨会话持久的记忆层。
  - 目前**未开工，优先级低**，视需要再启动。

（除此之外当前无计划中的待办；新想法随时往这里加。）

## 备注

- **commit / push 分工**：commit 由 AI 完成（本地、可逆）；**push 必须由用户本人执行**（外发 GitHub 由用户把关）。
- 每个待办收尾 = 实现 + 测试跑绿 + 在 PROGRESS.md 顶部追加一条变更日志（项目固定规矩）。
