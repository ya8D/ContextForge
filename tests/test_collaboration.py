"""
test_collaboration.py —— Coordinator → Workers → Reviewer 协作 Demo 的纯逻辑测试。

这些测试不调真实 API：用脚本化 Agent 驱动完整编排，确定性钉死并行、上下文隔离、
定向补充、最多一轮、失败状态、usage 分账和 team trace。真实模型端到端另放 test_e2e.py。
"""

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from contextforge.agent import Agent, AgentRunResult
from contextforge.collaboration import (
    Evidence,
    ReviewReport,
    TeamCoordinator,
    WorkerReport,
    WorkerTask,
    _WorkerCapture,
    _worker_tool,
    sum_usage,
    validate_worker_tasks,
)
from contextforge.tools import LocalTool, TOOL_FUNCTIONS, TOOL_SCHEMAS, tool_schemas_for


_USAGE = {
    "input_tokens": 1,
    "output_tokens": 2,
    "cache_creation_input_tokens": 3,
    "cache_read_input_tokens": 4,
}


class _Block:
    """够 Agent 主循环使用的最小 SDK content block 替身。"""

    def __init__(self, block_type, **kwargs):
        self.type = block_type
        self.__dict__.update(kwargs)

    def model_dump(self):
        data = dict(self.__dict__)
        data["type"] = self.type
        return data


class _Usage:
    input_tokens = 1
    output_tokens = 2
    cache_creation_input_tokens = 3
    cache_read_input_tokens = 4


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


def _result(output="完成", status="succeeded", *, trace_ref="trace/task_01", tool_calls=None):
    return AgentRunResult(
        status=status,
        output=output,
        usage=dict(_USAGE),
        trace_ref=trace_ref,
        duration_seconds=0.01,
        stop_reason="end_turn",
        tool_calls=list(tool_calls or []),
        error=None if status == "succeeded" else output,
        trace_metadata={},
    )


def _local_tool(tools, name):
    return next(tool for tool in tools if tool.name == name)


class _ScriptedAgent:
    """不调 API；按真实 tool_calls 形状驱动结构化证据门和完整编排。"""

    def __init__(self, owner, **kwargs):
        import copy

        self.owner = owner
        self.system_prompt = kwargs.get("system_prompt")
        self.local_tools = kwargs.get("local_tools") or []
        self.tool_schemas = copy.deepcopy(kwargs.get("tools") or [])
        self.compact_executor = kwargs.get("compact_executor")
        self.trace_metadata = dict(kwargs.get("trace_metadata") or {})
        self.messages = []
        self.read_files = set()
        self.run_count = 0
        self._tool_calls = []
        index = len(owner.instances)
        self.trace_dir = owner.root / f"run_{index:02d}"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        owner.instances.append(self)

    def tool_calls_snapshot(self):
        import copy
        return copy.deepcopy(self._tool_calls)

    def has_executed_tool(self, name, *, succeeded=False):
        return any(
            call["name"] == name and call.get("executed")
            and (not succeeded or call.get("succeeded"))
            for call in self._tool_calls
        )

    def _record(self, name, turn, *, path=None, succeeded=True):
        call = {
            "tool_use_id": f"{name}-{len(self._tool_calls)}",
            "name": name,
            "input": {"path": path} if path is not None else {},
            "turn": turn,
            "sequence": len(self._tool_calls),
            "allowed": True,
            "executed": True,
            "succeeded": succeeded,
            "reason": None,
        }
        if name == "read_file" and path is not None and succeeded:
            call["line_count"] = len(Path(path).read_text(encoding="utf-8").splitlines())
        self._tool_calls.append(call)
        return call

    def run_detailed(self, task):
        self.run_count += 1
        role = self.trace_metadata.get("role")
        phase = self.trace_metadata.get("phase")
        trace_ref = str(self.trace_dir / f"task_{self.run_count:02d}")

        if role == "coordinator" and phase == "planning":
            submit = _local_tool(self.local_tools, "submit_plan")
            self._record("submit_plan", 1)
            answer = submit.handler({"tasks": self.owner.plan})
            assert answer.startswith("[成功]")
            return _result("计划已提交", trace_ref=trace_ref, tool_calls=self._tool_calls)

        if role == "coordinator" and phase == "aggregation":
            self.owner.aggregation_payloads.append(task)
            return _result("最终汇总：TOKEN_W1 + TOKEN_W2", trace_ref=trace_ref)

        if role == "worker":
            worker_id = self.trace_metadata["worker_id"]
            attempt = self.trace_metadata["attempt"]
            self.owner.worker_assignments[worker_id].append(task)
            with self.owner.lock:
                self.owner.worker_calls[worker_id] += 1
                self.owner.active += 1
                self.owner.peak = max(self.owner.peak, self.owner.active)
            try:
                if attempt == 1:
                    self.owner.worker_barrier.wait(timeout=2)
            finally:
                with self.owner.lock:
                    self.owner.active -= 1

            if worker_id in self.owner.fail_workers:
                return _result("模拟 Worker 失败", status="failed", trace_ref=trace_ref)
            if attempt == 2 and worker_id in self.owner.fail_revision_workers:
                return _result("模拟补充失败", status="failed", trace_ref=trace_ref)

            path = str(self.owner.root / f"{worker_id}.py")
            self._record("read_file", 1, path=path)
            self._record("submit_worker_report", 2)
            submit = _local_tool(self.local_tools, "submit_worker_report")
            answer = submit.handler({
                "status": "succeeded",
                "summary": f"{worker_id} 在第 {attempt} 次完成",
                "evidence": [{
                    "path": path,
                    "line": attempt,
                    "claim": f"TOKEN_{worker_id.upper()}_A{attempt}",
                }],
                "error": "",
            })
            assert answer.startswith("[成功]"), answer
            return _result(
                f"{worker_id} 已提交", trace_ref=trace_ref, tool_calls=self._tool_calls
            )

        if role == "reviewer":
            self.owner.review_calls += 1
            index = self.owner.review_calls - 1
            if index in self.owner.fail_review_rounds:
                return _result("模拟 Reviewer 失败", status="failed", trace_ref=trace_ref)
            verdict, revise_ids = self.owner.review_script[min(index, len(self.owner.review_script) - 1)]
            submit = _local_tool(self.local_tools, "submit_review")
            capture = submit.handler.__self__
            if capture.evidence_paths:
                self._record("read_file", 1, path=sorted(capture.evidence_paths)[0])
            self._record("submit_review", 2)
            answer = submit.handler({
                "verdict": verdict,
                "feedback": f"第 {index + 1} 次审查",
                "revise_worker_ids": revise_ids,
            })
            assert answer.startswith("[成功]"), answer
            return _result(
                "审查已提交", trace_ref=trace_ref, tool_calls=self._tool_calls
            )

        raise AssertionError(f"未知角色/阶段：{role}/{phase}")


class _ScriptedFactory:
    def __init__(
        self,
        root: Path,
        *,
        review_script=None,
        fail_workers=None,
        fail_revision_workers=None,
        fail_review_rounds=None,
    ):
        self.root = root
        self.instances = []
        self.plan = [
            {
                "worker_id": "w1",
                "role": "源码分析",
                "instruction": "分析 w1.py",
                "expected_evidence": "给出路径、行号和 TOKEN_W1",
            },
            {
                "worker_id": "w2",
                "role": "测试分析",
                "instruction": "分析 w2.py",
                "expected_evidence": "给出路径、行号和 TOKEN_W2",
            },
        ]
        for worker_id in ("w1", "w2"):
            (root / f"{worker_id}.py").write_text(
                f"TOKEN_{worker_id.upper()}_A1\nTOKEN_{worker_id.upper()}_A2\n",
                encoding="utf-8",
            )
        self.review_script = review_script or [("accept", [])]
        self.fail_workers = set(fail_workers or [])
        self.fail_revision_workers = set(fail_revision_workers or [])
        self.fail_review_rounds = set(fail_review_rounds or [])
        self.worker_calls = Counter()
        self.worker_assignments = {"w1": [], "w2": []}
        self.review_calls = 0
        self.lock = threading.Lock()
        self.worker_barrier = threading.Barrier(2)
        self.active = 0
        self.peak = 0
        self.aggregation_payloads = []

    def __call__(self, **kwargs):
        return _ScriptedAgent(self, **kwargs)


def test_local_tool_does_not_pollute_global_registry():
    before_schema_names = [schema["name"] for schema in TOOL_SCHEMAS]
    before_function_names = set(TOOL_FUNCTIONS)

    local = LocalTool(
        name="submit_nested_demo",
        description="提交嵌套数组。",
        input_schema={
            "type": "object",
            "properties": {"tasks": {"type": "array", "items": {"type": "object"}}},
            "required": ["tasks"],
        },
        handler=lambda data: str(data),
    )

    assert local.schema["input_schema"]["properties"]["tasks"]["type"] == "array"
    assert [schema["name"] for schema in TOOL_SCHEMAS] == before_schema_names
    assert set(TOOL_FUNCTIONS) == before_function_names


def test_tool_schemas_for_is_a_strict_allowlist():
    assert [schema["name"] for schema in tool_schemas_for({"read_file"})] == ["read_file"]
    with pytest.raises(ValueError, match="未知工具"):
        tool_schemas_for({"read_file", "not_registered"})


def test_agent_passes_system_prompt_and_hard_blocks_hidden_tool(monkeypatch, tmp_path):
    calls = []
    responses = iter([
        _Response([_Block("tool_use", id="bad1", name="write_file",
                          input={"path": str(tmp_path / "x"), "content": "bad"})], "tool_use"),
        _Response([_Block("text", text="安全结束")], "end_turn"),
    ])

    local = LocalTool(
        name="submit_only",
        description="唯一允许的提交工具。",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda data: "ok",
    )
    agent = Agent(
        tools=[],
        local_tools=[local],
        system_prompt="ROLE_SYSTEM_SENTINEL",
        max_iterations=2,
        check_command=None,
    )

    def fake_create(**kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)
    executed = []
    original_execute = agent._execute_tool

    def guarded_execute(name, tool_input):
        executed.append(name)
        return original_execute(name, tool_input)

    monkeypatch.setattr(agent, "_execute_tool", guarded_execute)
    detail = agent.run_detailed("尝试调用隐藏工具")

    assert detail.status == "succeeded"
    assert calls[0]["system"] == "ROLE_SYSTEM_SENTINEL"
    assert all(call.get("tools") == [local.schema] for call in calls)
    assert detail.usage == _USAGE | {
        "input_tokens": 2,
        "output_tokens": 4,
        "cache_creation_input_tokens": 6,
        "cache_read_input_tokens": 8,
    }
    assert detail.tool_calls[0]["name"] == "write_file"
    assert detail.tool_calls[0]["allowed"] is False
    assert executed == [], "菜单外 write_file 不得进入执行器"
    denied = [
        block
        for message in agent.messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert denied and "未授权" in denied[0]["content"]


def test_run_detailed_distinguishes_incomplete_and_api_failure(monkeypatch):
    # 只有一轮且该轮仍请求工具 → 撞 max_iterations，必须是 incomplete，不是“非空即成功”。
    incomplete = Agent(tools=[], max_iterations=1, check_command=None)
    monkeypatch.setattr(
        incomplete.client.messages,
        "create",
        lambda **kwargs: _Response([
            _Block("tool_use", id="hidden", name="write_file", input={"path": "x", "content": "y"})
        ], "tool_use"),
    )
    detail = incomplete.run_detailed("触发轮数护栏")
    assert detail.status == "incomplete"
    assert detail.output.startswith("[未完成]")
    assert detail.error

    failed = Agent(tools=[], max_iterations=1, check_command=None)

    def raise_api(**kwargs):
        raise RuntimeError("API_DOWN_SENTINEL")

    monkeypatch.setattr(failed.client.messages, "create", raise_api)
    failed_detail = failed.run_detailed("触发 API 异常")
    assert failed_detail.status == "failed"
    assert failed_detail.output == ""
    assert "API_DOWN_SENTINEL" in failed_detail.error
    assert failed.messages == [], "run() 的失败回滚语义必须保持"


def test_worker_task_validation_rejects_bad_plans():
    valid = [
        WorkerTask("w1", "源码", "读 a.py", "路径和行号"),
        WorkerTask("w2", "测试", "读 test_a.py", "路径和行号"),
    ]
    assert validate_worker_tasks(valid) == valid

    with pytest.raises(ValueError, match="2～4"):
        validate_worker_tasks(valid[:1])
    with pytest.raises(ValueError, match="重复"):
        validate_worker_tasks([valid[0], valid[0]])
    with pytest.raises(ValueError, match="不能为空"):
        validate_worker_tasks([valid[0], WorkerTask("w2", "", "任务", "证据")])


def test_workers_really_overlap_and_keep_context_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(tmp_path)
    result = TeamCoordinator(agent_factory=factory, max_workers=2).run("并行分析两个文件")

    workers = [agent for agent in factory.instances if agent.trace_metadata.get("role") == "worker"]
    assert result.status == "succeeded"
    assert factory.peak >= 2, "两个 Worker 没有真实重叠执行"
    assert len(workers) == 2
    assert workers[0].messages is not workers[1].messages
    assert workers[0].read_files is not workers[1].read_files
    assert {report.worker_id for report in result.worker_reports} == {"w1", "w2"}
    assert "w2.py" not in factory.worker_assignments["w1"][0]
    assert "w1.py" not in factory.worker_assignments["w2"][0]


def test_reviewer_only_retries_named_worker_and_at_most_once(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(
        tmp_path,
        review_script=[("revise", ["w1"]), ("revise", ["w1"])],
    )
    result = TeamCoordinator(agent_factory=factory, max_workers=2).run("审查后定向补充")

    assert factory.worker_calls == Counter({"w1": 2, "w2": 1})
    assert factory.review_calls == 2
    assert result.status == "completed_with_issues"
    assert result.review.verdict == "revise"
    assert max(report.attempt for report in result.worker_reports if report.worker_id == "w1") == 2


def test_worker_failure_is_not_disguised_as_success(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(tmp_path, fail_workers={"w2"}, review_script=[("revise", ["w2"])] )
    result = TeamCoordinator(agent_factory=factory, max_workers=2).run("包含失败 Worker")

    failed = next(report for report in result.worker_reports if report.worker_id == "w2")
    assert failed.status == "failed"
    assert failed.error
    assert result.status != "succeeded"


def test_usage_is_summed_across_every_participant_run(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(tmp_path, review_script=[("revise", ["w1"]), ("accept", [])])
    result = TeamCoordinator(agent_factory=factory, max_workers=2).run("统计全团队 token")

    expected = {key: value * len(result.participant_runs) for key, value in _USAGE.items()}
    assert result.total_usage == expected
    assert result.total_usage == sum_usage(run.usage for run in result.participant_runs)


def test_production_team_trace_root_is_unique_across_processes(monkeypatch):
    """团队 trace 根必须含 PID，不能依赖每进程都会重置的 Agent 序号防跨进程碰撞。"""
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    coordinator = TeamCoordinator()
    team_id = coordinator._new_team_id()

    assert f"_{os.getpid()}_" in team_id


def test_team_manifest_links_each_agent_trace(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "on")
    factory = _ScriptedFactory(tmp_path)
    result = TeamCoordinator(agent_factory=factory, max_workers=2).run("生成 team trace")

    manifest_path = Path(result.trace_ref)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    participant_refs = {run.trace_ref for run in result.participant_runs}

    assert manifest_path.name == "team.json"
    assert manifest["team_id"] == result.team_id
    assert manifest["goal"] == "生成 team trace"
    assert {task["worker_id"] for task in manifest["tasks"]} == {"w1", "w2"}
    assert len(manifest["review_history"]) == 1
    assert {item["trace_ref"] for item in manifest["participant_runs"]} == participant_refs
    assert manifest["total_usage"] == result.total_usage
    assert all("messages" not in item for item in manifest["participant_runs"])
    assert all(item["duration_seconds"] >= 0 for item in manifest["participant_runs"])
    assert all(item["started_at"] <= item["finished_at"] for item in manifest["participant_runs"])


# ── 审查回归：通用 Agent 的结构化状态与权限边界 ───────────────

def test_non_end_stop_reasons_are_not_reported_as_success(monkeypatch):
    """截断、拒绝、暂停都不是完整交付，不能沿用默认 succeeded。"""
    expected = {
        "max_tokens": "incomplete",
        "pause_turn": "incomplete",
        "refusal": "failed",
    }
    for stop_reason, expected_status in expected.items():
        agent = Agent(tools=[], max_iterations=1, check_command=None)
        monkeypatch.setattr(
            agent.client.messages,
            "create",
            lambda _reason=stop_reason, **kwargs: _Response(
                [_Block("text", text=f"PARTIAL_{_reason}")], _reason
            ),
        )

        detail = agent.run_detailed(f"触发 {_reason if False else stop_reason}")

        assert detail.status == expected_status
        assert detail.stop_reason == stop_reason
        assert detail.error


def test_truncated_tool_use_is_audited_as_unexecuted(monkeypatch):
    """截断响应里的 tool_use 虽不执行，也必须留在结构化审计中。"""
    agent = Agent(tools=tool_schemas_for({"read_file"}), max_iterations=1, check_command=None)
    monkeypatch.setattr(
        agent.client.messages,
        "create",
        lambda **kwargs: _Response([
            _Block("tool_use", id="cut-read", name="read_file", input={"path": "partial.py"})
        ], "max_tokens"),
    )

    detail = agent.run_detailed("触发截断工具请求")

    assert detail.status == "incomplete"
    assert len(detail.tool_calls) == 1
    assert detail.tool_calls[0]["tool_use_id"] == "cut-read"
    assert detail.tool_calls[0]["allowed"] is True
    assert detail.tool_calls[0]["executed"] is False
    assert "max_tokens" in detail.tool_calls[0]["reason"]


def test_failed_run_rolls_back_read_file_authorization(monkeypatch, tmp_path):
    """任务失败后，已回滚的文件内容不能留下跨任务写权限。"""
    path = tmp_path / "protected.txt"
    path.write_text("旧内容", encoding="utf-8")
    responses = iter([
        _Response([
            _Block("tool_use", id="read1", name="read_file", input={"path": str(path)})
        ], "tool_use"),
    ])
    agent = Agent(max_iterations=2, check_command=None)

    def fake_create(**kwargs):
        try:
            return next(responses)
        except StopIteration:
            raise RuntimeError("SECOND_TURN_DOWN")

    monkeypatch.setattr(agent.client.messages, "create", fake_create)
    detail = agent.run_detailed("先读文件，随后 API 失败")

    assert detail.status == "failed"
    assert agent.read_files == set()
    denied = agent._execute_tool("write_file", {"path": str(path), "content": "不应写入"})
    assert denied.is_error is True
    assert path.read_text(encoding="utf-8") == "旧内容"


def test_tool_error_is_sent_with_anthropic_is_error(monkeypatch):
    """实例工具校验失败必须作为 is_error=true 回喂，而非普通结果。"""
    calls = []
    responses = iter([
        _Response([_Block("tool_use", id="bad-submit", name="submit_only", input={})], "tool_use"),
        _Response([_Block("text", text="已纠正")], "end_turn"),
    ])
    local = LocalTool(
        name="submit_only",
        description="总是拒绝的测试工具。",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda data: __import__("contextforge.tools", fromlist=["tool_error"]).tool_error(
            "[错误] TEST_REJECTED"
        ),
    )
    agent = Agent(tools=[], local_tools=[local], max_iterations=2, check_command=None)

    def fake_create(**kwargs):
        # Agent 会继续原地追加 messages；这里必须冻结“发出请求当刻”的载荷，不能保存活引用。
        import copy
        calls.append(copy.deepcopy(kwargs))
        return next(responses)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)
    detail = agent.run_detailed("触发实例工具错误")

    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "bad-submit"
    assert tool_result["is_error"] is True
    assert detail.tool_calls[0]["succeeded"] is False


def test_terminal_local_tool_can_complete_on_last_iteration(monkeypatch):
    """终态结构化提交成功后立即结束，不能因缺少多余 end_turn 轮而误报 incomplete。"""
    local = LocalTool(
        name="submit_terminal",
        description="提交终态。",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda data: __import__("contextforge.tools", fromlist=["tool_success"]).tool_success(
            "[成功] 已提交"
        ),
        terminal=True,
    )
    agent = Agent(
        tools=[], local_tools=[local], max_iterations=1, check_command=None
    )
    monkeypatch.setattr(
        agent.client.messages,
        "create",
        lambda **kwargs: _Response([
            _Block("tool_use", id="terminal1", name="submit_terminal", input={})
        ], "tool_use"),
    )

    detail = agent.run_detailed("最后一轮提交")

    assert detail.status == "succeeded"
    assert detail.tool_calls[0]["succeeded"] is True
    assert detail.stop_reason == "tool_use"


def test_local_tool_cannot_impersonate_any_global_tool():
    """即使基础菜单为空，实例工具也不能冒用全局敏感工具名。"""
    local = LocalTool(
        name="run_command",
        description="冒名工具。",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda data: "不应执行",
    )
    with pytest.raises(ValueError, match="全局工具重名"):
        Agent(tools=[], local_tools=[local], check_command=None)


def test_worker_evidence_requires_earlier_matching_read(monkeypatch, tmp_path):
    """Worker 不能读诱饵文件后提交另一文件，也不能同轮先猜报告再读。"""
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    target = tmp_path / "target.py"
    decoy = tmp_path / "decoy.py"
    target.write_text("TARGET = 1\n", encoding="utf-8")
    decoy.write_text("DECOY = 1\n", encoding="utf-8")

    capture = _WorkerCapture()
    agent = Agent(
        tools=tool_schemas_for({"read_file"}),
        local_tools=[_worker_tool(capture)],
        check_command=None,
    )
    capture.bind(agent)
    agent._last_run_tool_calls = [
        {
            "name": "read_file", "input": {"path": str(decoy)}, "turn": 1,
            "executed": True, "succeeded": True,
        },
        {
            "name": "submit_worker_report", "input": {}, "turn": 2,
            "executed": True, "succeeded": False,
        },
    ]
    result = capture.submit({
        "status": "succeeded",
        "summary": "伪造 target 结论",
        "evidence": [{"path": str(target), "line": 1, "claim": "TARGET = 1"}],
        "error": "",
    })
    assert result.is_error is True
    assert capture.report is None

    agent._last_run_tool_calls[0]["input"]["path"] = str(target)
    agent._last_run_tool_calls[0]["turn"] = 2
    result = capture.submit({
        "status": "succeeded",
        "summary": "同轮猜测",
        "evidence": [{"path": str(target), "line": 1, "claim": "TARGET = 1"}],
        "error": "",
    })
    assert result.is_error is True
    assert capture.report is None


def test_worker_evidence_rejects_line_beyond_successful_read(monkeypatch, tmp_path):
    """真实 read_file 的行数审计必须拦住超出文件范围的 evidence.line。"""
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    target = tmp_path / "two_lines.py"
    target.write_text("FIRST = 1\nSECOND = 2\n", encoding="utf-8")
    capture = _WorkerCapture()
    agent = Agent(
        tools=tool_schemas_for({"read_file"}),
        local_tools=[_worker_tool(capture)],
        check_command=None,
    )
    capture.bind(agent)
    agent._last_run_tool_calls = [
        {
            "name": "read_file", "input": {"path": str(target)}, "turn": 1,
            "executed": True, "succeeded": True, "line_count": 2,
        },
        {
            "name": "submit_worker_report", "input": {}, "turn": 2,
            "executed": True, "succeeded": False,
        },
    ]

    result = capture.submit({
        "status": "succeeded",
        "summary": "伪造第三行",
        "evidence": [{"path": str(target), "line": 3, "claim": "THIRD = 3"}],
        "error": "",
    })

    assert result.is_error is True
    assert "line 越界" in str(result)
    assert capture.report is None


def test_revision_failure_keeps_previous_evidence(monkeypatch, tmp_path):
    """定向补充失败时，首轮已核实证据不能被空失败报告覆盖。"""
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(
        tmp_path,
        review_script=[("revise", ["w1"])],
        fail_revision_workers={"w1"},
    )
    result = TeamCoordinator(agent_factory=factory, max_workers=2).run("补充失败仍保留旧证据")

    w1_reports = [report for report in result.worker_reports if report.worker_id == "w1"]
    assert len(w1_reports) == 2
    assert w1_reports[0].evidence
    assert w1_reports[1].evidence == w1_reports[0].evidence
    assert w1_reports[1].status == "failed"
    assert w1_reports[1].error


def test_reviewer_failure_does_not_rerun_workers(monkeypatch, tmp_path):
    """Reviewer 基础设施失败不是业务 revise，不能消耗唯一 Worker 补充轮。"""
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(tmp_path, fail_review_rounds={0})
    result = TeamCoordinator(agent_factory=factory, max_workers=2).run("Reviewer 失败")

    assert result.status == "failed"
    assert factory.worker_calls == Counter({"w1": 1, "w2": 1})
    assert factory.review_calls == 1


def test_worker_orchestration_exception_keeps_sibling_results(monkeypatch, tmp_path):
    """一个 Worker 构造异常不应击穿整个 fan-out 或抹掉兄弟报告。"""
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(tmp_path, review_script=[("revise", ["w2"])])

    class NoopBarrier:
        def wait(self, timeout=None):
            return 0

    # w2 在 Agent 构造前失败，故本用例关闭仅用于“正常双 Worker 重叠”证明的 Barrier。
    factory.worker_barrier = NoopBarrier()
    original = factory.__call__

    class RaisingFactory:
        def __call__(self, **kwargs):
            if (kwargs.get("trace_metadata") or {}).get("worker_id") == "w2":
                raise RuntimeError("WORKER_FACTORY_DOWN")
            return original(**kwargs)

    coordinator = TeamCoordinator(agent_factory=RaisingFactory(), max_workers=2)
    result = coordinator.run("保留兄弟结果")

    reports = {report.worker_id: report for report in result.worker_reports if report.attempt == 1}
    assert reports["w1"].status == "succeeded"
    assert reports["w2"].status == "failed"
    failed_run = next(run for run in result.participant_runs if run.worker_id == "w2")
    assert failed_run.parent_id is not None


def test_aggregation_payload_excludes_control_plane_telemetry(monkeypatch, tmp_path):
    """Reviewer/汇总模型只收业务 DTO，不重复发送 usage、trace、时间戳和 tool_calls。"""
    monkeypatch.setenv("CONTEXTFORGE_TRACE", "off")
    factory = _ScriptedFactory(tmp_path)
    TeamCoordinator(agent_factory=factory, max_workers=2).run("精简模型载荷")

    payload = factory.aggregation_payloads[-1]
    for forbidden in ("usage", "trace_ref", "started_at", "finished_at", "tool_calls"):
        assert f'"{forbidden}"' not in payload
