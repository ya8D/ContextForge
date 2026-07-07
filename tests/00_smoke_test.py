"""
00_smoke_test.py —— 冒烟测试：验证 Anthropic API 通路是否可用。

这是 myagent 项目的第一个文件，目的只有一个：
用最少的代码，确认「我们能成功调用一次模型并拿到回复」。

关键点（对照 agent_learning 第 2.4 节 模型 API 调用入门）：
- 凭据、地址、模型来自 myagent/.env（由 python-dotenv 加载进环境）。
  这套代理凭据本由 VSCode 扩展注入其子进程，普通终端拿不到，故落到 .env。
- anthropic SDK 自动读取环境里的 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL。
- 模型 ID 从环境变量 ANTHROPIC_MODEL 读，绝不写死（见 CLAUDE.md）。
"""

import os
import sys

import anthropic
from dotenv import load_dotenv

# 加载 myagent/.env（代理凭据）。tests/ 在下一层，故往上找一级。
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def main() -> int:
    # 从环境读模型 ID；给一个兜底值，避免环境没设时直接崩。
    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8[1m]")

    # SDK 自动读取 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL，无需显式传参。
    client = anthropic.Anthropic()

    # 一次最小的对话请求：让模型只回一个词，方便肉眼确认。
    resp = client.messages.create(
        model=model,
        max_tokens=50,
        messages=[
            {"role": "user", "content": "我说 ping，你只回一个词：pong"}
        ],
    )

    # resp.content 是一个 content block 列表；纯文本回复取第一个块的 .text。
    reply = resp.content[0].text

    print(f"使用模型   : {model}")
    print(f"模型回复   : {reply}")
    print(f"stop_reason: {resp.stop_reason}")
    print(f"token 用量 : {resp.usage.input_tokens} in / {resp.usage.output_tokens} out")
    print("✅ API 通路正常" if reply else "⚠️ 收到空回复")
    return 0


if __name__ == "__main__":
    sys.exit(main())
