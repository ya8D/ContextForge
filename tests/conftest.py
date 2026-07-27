"""测试基础设施：默认隔离 Token Counting 网络副作用。

纯逻辑测试中的 Agent 往往只替换 ``messages.create`` 来脚本化模型响应。生产代码现在每轮还会
调用 ``messages.count_tokens``；若不统一隔离，``-m 'not e2e'`` 会在有凭据机器上悄悄访问真实 API，
在离线 CI 则失败。e2e 测试明确保留真实端点，其余测试默认返回远低于软/硬阈值的稳定估算；专门
验证 preflight 的测试仍可在用例内 monkeypatch 覆盖这个实例方法。
"""

import pytest


class _OfflineTokenCount:
    input_tokens = 1


@pytest.fixture(autouse=True)
def _isolate_token_counting(request, monkeypatch):
    if request.node.get_closest_marker("e2e") is not None:
        return

    import anthropic

    monkeypatch.setattr(
        anthropic.resources.Messages,
        "count_tokens",
        lambda self, **kwargs: _OfflineTokenCount(),
    )
