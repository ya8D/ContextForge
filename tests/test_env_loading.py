"""项目根 .env 与父进程环境的优先级测试。"""

import os

from contextforge.agent import _load_project_dotenv


def test_project_dotenv_overrides_injected_anthropic_model(monkeypatch, tmp_path):
    """IDE/父进程注入的模型不能盖过项目 .env 中的显式选择。"""
    dotenv = tmp_path / ".env"
    dotenv.write_text("ANTHROPIC_MODEL=PROJECT_MODEL_SENTINEL\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_MODEL", "PARENT_PROCESS_MODEL_SENTINEL")

    _load_project_dotenv(dotenv)

    assert os.environ["ANTHROPIC_MODEL"] == "PROJECT_MODEL_SENTINEL"


def test_project_dotenv_does_not_override_runtime_log_switch(monkeypatch, tmp_path):
    """只提升 Anthropic 三项优先级；临时 CONTEXTFORGE_* 开关仍以进程环境为准。"""
    dotenv = tmp_path / ".env"
    dotenv.write_text("CONTEXTFORGE_LOG=off\n", encoding="utf-8")
    monkeypatch.setenv("CONTEXTFORGE_LOG", "debug")

    _load_project_dotenv(dotenv)

    assert os.environ["CONTEXTFORGE_LOG"] == "debug"
