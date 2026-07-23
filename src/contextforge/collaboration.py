"""
collaboration.py —— Coordinator → Workers → Reviewer 多 Agent 协作 Demo。

对照 agent_learning 第 16 章（多 Agent 协作）：
- 第 16.2 节：角色间只传结构化任务、证据与审查反馈；
- 第 16.3 节：每个 Worker / Reviewer 使用独立上下文；
- 第 16.4 节：Python Coordinator 掌握并发、返工上限和完成判定；
- 第 16.5 节：plan → fan-out → review → 一次定向补充 → fan-in。

LLM 负责语义拆分、只读调查、证据审查和自然语言汇总；Python 负责确定性 TAOR
编排。刻意不做 DAG、checkpoint、长期记忆、分布式或并发写文件。
"""

import itertools
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from contextforge.agent import Agent, AgentRunResult
from contextforge.tools import (
    LocalTool,
    _norm,
    tool_error,
    tool_schemas_for,
    tool_success,
)


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@dataclass(frozen=True)
class Evidence:
    """Worker 亲自读取后提交的一条可定位证据。"""

    path: str
    line: int
    claim: str


@dataclass(frozen=True)
class WorkerTask:
    """Coordinator 分给一个 Worker 的窄任务。"""

    worker_id: str
    role: str
    instruction: str
    expected_evidence: str


@dataclass
class WorkerReport:
    """一个 Worker 某次尝试的结构化交付。"""

    worker_id: str
    status: str
    summary: str
    evidence: list[Evidence]
    trace_ref: str | None
    usage: dict[str, int]
    attempt: int
    started_at: float
    finished_at: float
    error: str | None = None


@dataclass
class ReviewReport:
    """Reviewer 对当前最新 WorkerReports 的独立审查。"""

    verdict: str
    feedback: str
    revise_worker_ids: list[str]
    trace_ref: str | None
    usage: dict[str, int]
    tool_calls: list[dict]
    round: int = 1
    error: str | None = None


@dataclass
class ParticipantRun:
    """一次角色 TAOR 运行的分账与 trace 索引。"""

    agent_id: str
    parent_id: str | None
    role: str
    worker_id: str | None
    attempt: int
    phase: str
    status: str
    started_at: float
    finished_at: float
    duration_seconds: float
    usage: dict[str, int]
    tool_calls: list[dict]
    trace_ref: str | None
    error: str | None


@dataclass
class TeamResult:
    """一次完整协作链的业务结果与控制面索引。"""

    team_id: str
    status: str
    final_answer: str
    worker_reports: list[WorkerReport]
    review: ReviewReport | None
    total_usage: dict[str, int]
    participant_runs: list[ParticipantRun]
    trace_ref: str | None


def _empty_usage() -> dict[str, int]:
    return {key: 0 for key in _USAGE_KEYS}


def sum_usage(usages: Iterable[dict[str, int]]) -> dict[str, int]:
    """聚合所有角色/尝试的四项 usage；缺失或 None 均按 0。"""
    total = _empty_usage()
    for usage in usages:
        for key in total:
            total[key] += (usage or {}).get(key) or 0
    return total


def validate_worker_tasks(tasks: list[WorkerTask]) -> list[WorkerTask]:
    """校验 Coordinator 计划：2～4 个独立任务、ID 唯一、字段非空。"""
    if not 2 <= len(tasks) <= 4:
        raise ValueError("Worker 计划必须包含 2～4 个任务")
    ids = [task.worker_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("worker_id 不能重复")
    for task in tasks:
        values = (task.worker_id, task.role, task.instruction, task.expected_evidence)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("WorkerTask 的 worker_id/role/instruction/expected_evidence 均不能为空")
    return tasks


def _team_log(tag: str, message: str, *, error: bool = False) -> None:
    setting = os.environ.get("CONTEXTFORGE_LOG", "normal").strip().lower()
    if setting not in {"off", "normal", "debug"}:
        setting = "normal"
    if error or setting != "off":
        print(f"[CF] {tag} {message}")


def _trace_enabled() -> bool:
    return os.environ.get("CONTEXTFORGE_TRACE", "on").strip().lower() != "off"


def _require_string(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    return value.strip()


def _require_string_list(value, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是字符串数组")
    values = [_require_string(item, field) for item in value]
    if not allow_empty and not values:
        raise ValueError(f"{field} 不能为空")
    return values


def _require_exact_keys(data: dict, expected: set[str]) -> None:
    if not isinstance(data, dict):
        raise ValueError("提交参数必须是 object")
    extra = set(data) - expected
    missing = expected - set(data)
    if missing:
        raise ValueError(f"缺少字段：{', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"不允许额外字段：{', '.join(sorted(extra))}")


def _evidence_from_data(value) -> list[Evidence]:
    if not isinstance(value, list) or not value:
        raise ValueError("evidence 必须是非空数组")
    evidence = []
    for item in value:
        _require_exact_keys(item, {"path", "line", "claim"})
        path = _require_string(item["path"], "evidence.path")
        line = item["line"]
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ValueError("evidence.line 必须是正整数")
        claim = _require_string(item["claim"], "evidence.claim")
        evidence.append(Evidence(path=path, line=line, claim=claim))
    return evidence


def _successful_reads_before(agent, submit_tool: str) -> dict[str, int | None]:
    """返回严格早于结构化提交轮的读取路径及当时所见行数。"""
    calls = agent.tool_calls_snapshot()
    submits = [call for call in calls if call["name"] == submit_tool]
    if not submits:
        return {}
    submit_turn = submits[-1].get("turn", 0)
    reads = {}
    for call in calls:
        if (call["name"] == "read_file" and call.get("executed")
                and call.get("succeeded") and call.get("turn", 0) < submit_turn):
            path = (call.get("input") or {}).get("path")
            if isinstance(path, str):
                reads[_norm(path)] = call.get("line_count")
    return reads


def _evidence_model(evidence: Evidence) -> dict:
    return {"path": evidence.path, "line": evidence.line, "claim": evidence.claim}


def _worker_model(report: WorkerReport) -> dict:
    """只把业务信息交给模型，usage/trace/时间戳留在控制面。"""
    return {
        "worker_id": report.worker_id,
        "status": report.status,
        "summary": report.summary,
        "evidence": [_evidence_model(item) for item in report.evidence],
        "error": report.error,
    }


def _review_model(report: ReviewReport) -> dict:
    return {
        "verdict": report.verdict,
        "feedback": report.feedback,
        "revise_worker_ids": list(report.revise_worker_ids),
        "error": report.error,
    }


class _PlanCapture:
    def __init__(self):
        self.tasks: list[WorkerTask] | None = None

    def submit(self, data: dict):
        if self.tasks is not None:
            return tool_error("[错误] 计划已经提交，不得重复覆盖")
        try:
            _require_exact_keys(data, {"tasks"})
            raw_tasks = data["tasks"]
            if not isinstance(raw_tasks, list):
                raise ValueError("tasks 必须是数组")
            tasks = []
            for item in raw_tasks:
                _require_exact_keys(
                    item, {"worker_id", "role", "instruction", "expected_evidence"}
                )
                tasks.append(WorkerTask(
                    worker_id=_require_string(item["worker_id"], "worker_id"),
                    role=_require_string(item["role"], "role"),
                    instruction=_require_string(item["instruction"], "instruction"),
                    expected_evidence=_require_string(
                        item["expected_evidence"], "expected_evidence"
                    ),
                ))
            self.tasks = validate_worker_tasks(tasks)
        except (AttributeError, TypeError, ValueError) as exc:
            return tool_error(f"[错误] 计划未接收：{exc}。请修正后重新调用 submit_plan。")
        return tool_success(f"[成功] 已接收 {len(self.tasks)} 个 WorkerTask。")


class _WorkerCapture:
    def __init__(self):
        self.agent = None
        self.report: dict | None = None

    def bind(self, agent) -> None:
        self.agent = agent

    def submit(self, data: dict):
        if self.report is not None:
            return tool_error("[错误] WorkerReport 已提交，不得重复覆盖")
        try:
            _require_exact_keys(data, {"status", "summary", "evidence", "error"})
            status = data["status"]
            if status not in {"succeeded", "failed"}:
                raise ValueError("status 只能是 succeeded 或 failed")
            summary = _require_string(data["summary"], "summary")
            error_raw = data["error"]
            if not isinstance(error_raw, str):
                raise ValueError("error 必须是字符串")

            if status == "succeeded":
                if error_raw.strip():
                    raise ValueError("succeeded 状态的 error 必须为空")
                evidence = _evidence_from_data(data["evidence"])
                if self.agent is None:
                    raise ValueError("Worker 工具尚未绑定 Agent")
                reads = _successful_reads_before(self.agent, "submit_worker_report")
                missing = [item.path for item in evidence if _norm(item.path) not in reads]
                if missing:
                    raise ValueError(
                        "以下 evidence 文件未在更早 TAOR 轮成功读取：" + ", ".join(missing)
                    )
                for item in evidence:
                    line_count = reads[_norm(item.path)]
                    if line_count is not None and item.line > line_count:
                        raise ValueError(
                            f"evidence.line 越界：{item.path}:{item.line}（读取时共 {line_count} 行）"
                        )
                error = None
            else:
                if data["evidence"] not in ([], None):
                    raise ValueError("failed 状态的 evidence 必须为空数组")
                evidence = []
                error = _require_string(error_raw, "failed 状态的 error")

            self.report = {
                "status": status,
                "summary": summary,
                "evidence": evidence,
                "error": error,
            }
        except (AttributeError, TypeError, ValueError) as exc:
            return tool_error(
                f"[错误] 报告未接收：{exc}。请读取证据并修正后重新提交。"
            )
        return tool_success("[成功] WorkerReport 已接收。")


class _ReviewCapture:
    def __init__(self, reports: dict[str, WorkerReport]):
        self.agent = None
        self.worker_ids = set(reports)
        self.failed_ids = {
            worker_id for worker_id, report in reports.items() if report.status != "succeeded"
        }
        self.evidence_paths = {
            _norm(evidence.path)
            for report in reports.values()
            for evidence in report.evidence
        }
        self.report: dict | None = None

    def bind(self, agent) -> None:
        self.agent = agent

    def submit(self, data: dict):
        if self.report is not None:
            return tool_error("[错误] ReviewReport 已提交，不得重复覆盖")
        try:
            _require_exact_keys(data, {"verdict", "feedback", "revise_worker_ids"})
            verdict = data["verdict"]
            if verdict not in {"accept", "revise"}:
                raise ValueError("verdict 只能是 accept 或 revise")
            feedback = _require_string(data["feedback"], "feedback")
            revise_ids = _require_string_list(
                data["revise_worker_ids"], "revise_worker_ids", allow_empty=True
            )
            if len(revise_ids) != len(set(revise_ids)):
                raise ValueError("revise_worker_ids 不能重复")
            unknown = set(revise_ids) - self.worker_ids
            if unknown:
                raise ValueError(f"未知 worker_id：{', '.join(sorted(unknown))}")
            if verdict == "accept" and revise_ids:
                raise ValueError("accept 时 revise_worker_ids 必须为空")
            if verdict == "accept" and self.failed_ids:
                raise ValueError(
                    "不能 accept 失败 Worker，必须 revise：" + ", ".join(sorted(self.failed_ids))
                )
            if verdict == "revise" and not revise_ids:
                raise ValueError("revise 时必须点名至少一个 worker_id")
            missing_failed = self.failed_ids - set(revise_ids)
            if missing_failed:
                raise ValueError(
                    "revise 必须包含失败 Worker：" + ", ".join(sorted(missing_failed))
                )

            # 有可核验证据时，Reviewer 必须在提交前的更早 TAOR 轮回读其中至少一个路径。
            if self.evidence_paths:
                if self.agent is None:
                    raise ValueError("Reviewer 工具尚未绑定 Agent")
                read_paths = set(_successful_reads_before(self.agent, "submit_review"))
                if not (read_paths & self.evidence_paths):
                    raise ValueError("必须先在更早 TAOR 轮回读至少一个 Worker evidence 文件")

            self.report = {
                "verdict": verdict,
                "feedback": feedback,
                "revise_worker_ids": revise_ids,
            }
        except (AttributeError, TypeError, ValueError) as exc:
            return tool_error(f"[错误] 审查未接收：{exc}。请修正后重新调用 submit_review。")
        return tool_success("[成功] ReviewReport 已接收。")


def _plan_tool(capture: _PlanCapture) -> LocalTool:
    task_schema = {
        "type": "object",
        "properties": {
            "worker_id": {"type": "string", "description": "短且唯一，如 source/tests/docs"},
            "role": {"type": "string", "description": "Worker 专长角色"},
            "instruction": {"type": "string", "description": "自包含的只读调查任务"},
            "expected_evidence": {"type": "string", "description": "必须交回的文件路径和事实"},
        },
        "required": ["worker_id", "role", "instruction", "expected_evidence"],
        "additionalProperties": False,
    }
    return LocalTool(
        name="submit_plan",
        description="提交 2～4 个可并行、互不重叠的只读 WorkerTask。",
        input_schema={
            "type": "object",
            "properties": {"tasks": {"type": "array", "items": task_schema}},
            "required": ["tasks"],
            "additionalProperties": False,
        },
        handler=capture.submit,
        terminal=True,
    )


def _worker_tool(capture: _WorkerCapture) -> LocalTool:
    evidence_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer"},
            "claim": {"type": "string"},
        },
        "required": ["path", "line", "claim"],
        "additionalProperties": False,
    }
    return LocalTool(
        name="submit_worker_report",
        description="提交本 Worker 的最终结构化报告。",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["succeeded", "failed"]},
                "summary": {"type": "string"},
                "evidence": {"type": "array", "items": evidence_schema},
                "error": {"type": "string"},
            },
            "required": ["status", "summary", "evidence", "error"],
            "additionalProperties": False,
        },
        handler=capture.submit,
        terminal=True,
    )


def _review_tool(capture: _ReviewCapture) -> LocalTool:
    return LocalTool(
        name="submit_review",
        description="回读证据后提交独立审查结论。",
        input_schema={
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["accept", "revise"]},
                "feedback": {"type": "string"},
                "revise_worker_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "feedback", "revise_worker_ids"],
            "additionalProperties": False,
        },
        handler=capture.submit,
        terminal=True,
    )


def _merge_revision(previous: WorkerReport | None, current: WorkerReport) -> WorkerReport:
    """把定向补充合入旧报告；补充失败也保留首轮已核实证据。"""
    if previous is None:
        return current
    # 同一路径同一行的新证据视为修正，替换旧 claim；其它证据才做增量追加。
    merged = {(_norm(item.path), item.line): item for item in previous.evidence}
    for item in current.evidence:
        merged[(_norm(item.path), item.line)] = item
    evidence = list(merged.values())
    summary = previous.summary
    if current.summary and current.summary != previous.summary:
        summary += "\n补充：" + current.summary
    error = current.error
    return WorkerReport(
        worker_id=current.worker_id,
        status=current.status,
        summary=summary,
        evidence=evidence,
        trace_ref=current.trace_ref,
        usage=current.usage,
        attempt=current.attempt,
        started_at=current.started_at,
        finished_at=current.finished_at,
        error=error,
    )


class TeamCoordinator:
    """驱动固定的 plan → parallel work → review → optional revise → aggregate。"""

    _team_seq = itertools.count()
    _factory_lock = threading.Lock()

    def __init__(self, agent_factory: Callable[..., Agent] = Agent, max_workers: int = 4):
        self.agent_factory = agent_factory
        self.max_workers = max(1, max_workers)
        self.participant_runs: list[ParticipantRun] = []
        self.worker_reports: list[WorkerReport] = []
        self.review_history: list[ReviewReport] = []
        self._goal = ""
        self._tasks: list[WorkerTask] = []
        self._active_workers = 0
        self._active_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._current_team_id: str | None = None
        self._trace_root: Path | None = None

    def _new_team_id(self) -> str:
        now = datetime.now()
        return f"team_{now:%Y%m%d_%H%M%S}_{os.getpid()}_{next(self._team_seq):04d}"

    def _new_agent(self, **kwargs):
        """串行调用可注入 factory，并拒绝跨角色复用同一个有状态 Agent。"""
        with self._factory_lock:
            agent = self.agent_factory(**kwargs)
        # 生产 Agent 的默认 trace_dir 以本次团队根为锚，避免多进程同秒下进程内序号重置
        # 后目录碰撞；注入的测试替身保留自己的 trace_dir，不强行改写。
        if (self.agent_factory is Agent and self._trace_root is not None
                and isinstance(agent, Agent)):
            agent.trace_dir = self._trace_root / f"agent_{len(self._agents):02d}"
            if _trace_enabled():
                agent.trace_dir.mkdir(parents=True, exist_ok=True)
        if any(existing is agent for existing in getattr(self, "_agents", [])):
            raise RuntimeError("agent_factory 必须为每个角色返回独立 Agent 实例")
        self._agents.append(agent)
        return agent

    def _run_agent(
        self,
        agent,
        task: str,
        *,
        role: str,
        phase: str,
        worker_id: str | None = None,
        attempt: int = 1,
        parent_id: str | None = None,
    ) -> tuple[AgentRunResult, ParticipantRun]:
        started = time.time()
        detail = agent.run_detailed(task)
        finished = time.time()
        agent_id = getattr(agent, "trace_dir", Path(f"{role}_{id(agent)}")).name
        participant = ParticipantRun(
            agent_id=agent_id,
            parent_id=parent_id,
            role=role,
            worker_id=worker_id,
            attempt=attempt,
            phase=phase,
            status=detail.status,
            started_at=started,
            finished_at=finished,
            duration_seconds=detail.duration_seconds,
            usage=dict(detail.usage),
            tool_calls=list(detail.tool_calls),
            trace_ref=detail.trace_ref,
            error=detail.error,
        )
        return detail, participant

    def _failed_worker_pair(
        self,
        task: WorkerTask,
        attempt: int,
        started: float,
        exc: Exception,
        parent_id: str | None = None,
    ) -> tuple[WorkerReport, ParticipantRun]:
        finished = time.time()
        error = f"{type(exc).__name__}: {exc}"
        usage = _empty_usage()
        participant = ParticipantRun(
            agent_id=f"worker_{task.worker_id}_{attempt}_failed",
            parent_id=parent_id,
            role="worker",
            worker_id=task.worker_id,
            attempt=attempt,
            phase="work" if attempt == 1 else "revision",
            status="failed",
            started_at=started,
            finished_at=finished,
            duration_seconds=finished - started,
            usage=usage,
            tool_calls=[],
            trace_ref=None,
            error=error,
        )
        report = WorkerReport(
            worker_id=task.worker_id,
            status="failed",
            summary="Worker 编排异常",
            evidence=[],
            trace_ref=None,
            usage=usage,
            attempt=attempt,
            started_at=started,
            finished_at=finished,
            error=error,
        )
        return report, participant

    def _run_worker(
        self,
        team_id: str,
        parent_id: str,
        task: WorkerTask,
        attempt: int,
        feedback: str | None = None,
        previous: WorkerReport | None = None,
    ) -> tuple[WorkerReport, ParticipantRun]:
        capture = _WorkerCapture()
        agent = self._new_agent(
            tools=tool_schemas_for({"read_file"}),
            local_tools=[_worker_tool(capture)],
            system_prompt=(
                f"你是多 Agent 团队中的只读 Worker，角色是「{task.role}」。"
                "你只处理自己的窄任务。必须先在一个 TAOR 轮调用 read_file，观察结果后再在后续轮"
                "调用 submit_worker_report。evidence 必须逐项提交 path、正整数 line、claim。"
                "禁止写文件、跑命令或派生 Agent。"
            ),
            trace_metadata={
                "team_id": team_id,
                "parent_id": parent_id,
                "role": "worker",
                "worker_id": task.worker_id,
                "attempt": attempt,
            },
            max_iterations=12,
            check_command=None,
            compact_executor="self",
        )
        capture.bind(agent)
        with self._active_lock:
            self._active_workers += 1
            active = self._active_workers
        _team_log("👷 [Worker]", f"{task.worker_id} 第 {attempt} 次启动（当前并行 {active}）")
        assignment = (
            f"你的 worker_id：{task.worker_id}\n"
            f"你的任务：{task.instruction}\n"
            f"预期证据：{task.expected_evidence}"
        )
        if previous:
            assignment += "\n\n上一版报告：\n" + json.dumps(
                _worker_model(previous), ensure_ascii=False
            )
        if feedback:
            assignment += f"\n\nReviewer 定向反馈：{feedback}\n只补缺口，不要扩大范围。"
        try:
            detail, participant = self._run_agent(
                agent,
                assignment,
                role="worker",
                phase="work" if attempt == 1 else "revision",
                worker_id=task.worker_id,
                attempt=attempt,
                parent_id=parent_id,
            )
        finally:
            with self._active_lock:
                self._active_workers -= 1

        submitted = capture.report
        if detail.status != "succeeded" or submitted is None:
            reasons = []
            if detail.status != "succeeded":
                reasons.append(detail.error or f"Worker 状态={detail.status}")
            if submitted is None:
                reasons.append("未提交合法 WorkerReport")
            report = WorkerReport(
                worker_id=task.worker_id,
                status="failed",
                summary=detail.output or "Worker 未完成",
                evidence=[],
                trace_ref=detail.trace_ref,
                usage=dict(detail.usage),
                attempt=attempt,
                started_at=participant.started_at,
                finished_at=participant.finished_at,
                error="；".join(reasons),
            )
        else:
            report = WorkerReport(
                worker_id=task.worker_id,
                status=submitted["status"],
                summary=submitted["summary"],
                evidence=list(submitted["evidence"]),
                trace_ref=detail.trace_ref,
                usage=dict(detail.usage),
                attempt=attempt,
                started_at=participant.started_at,
                finished_at=participant.finished_at,
                error=submitted["error"],
            )
        report = _merge_revision(previous, report)
        participant.status = report.status
        participant.error = report.error
        _team_log("✅ [Worker]", f"{task.worker_id} 第 {attempt} 次结束：{report.status}")
        return report, participant

    def _run_worker_guarded(self, *args, task: WorkerTask, attempt: int, **kwargs):
        started = time.time()
        try:
            return self._run_worker(*args, task=task, attempt=attempt, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 单 Worker 故障不得抹掉兄弟结果
            _team_log("⚠️ [Worker]", f"{task.worker_id} 编排异常：{exc}", error=True)
            parent_id = args[1] if len(args) > 1 else kwargs.get("parent_id")
            return self._failed_worker_pair(
                task, attempt, started, exc, parent_id=parent_id
            )

    def _run_workers(
        self,
        team_id: str,
        parent_id: str,
        tasks: list[WorkerTask],
        attempt: int,
        feedback: str | None = None,
        previous_reports: dict[str, WorkerReport] | None = None,
    ) -> list[WorkerReport]:
        if not tasks:
            return []

        def run_one(task: WorkerTask):
            previous = (previous_reports or {}).get(task.worker_id)
            return self._run_worker_guarded(
                team_id,
                parent_id,
                task=task,
                attempt=attempt,
                feedback=feedback,
                previous=previous,
            )

        if len(tasks) == 1:
            pairs = [run_one(tasks[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks))) as pool:
                pairs = list(pool.map(run_one, tasks))
        reports = [pair[0] for pair in pairs]
        self.participant_runs.extend(pair[1] for pair in pairs)
        self.worker_reports.extend(reports)
        return reports

    def _run_review(
        self,
        team_id: str,
        parent_id: str,
        goal: str,
        tasks: list[WorkerTask],
        latest_reports: dict[str, WorkerReport],
        round_number: int,
    ) -> ReviewReport:
        capture = _ReviewCapture(latest_reports)
        agent = self._new_agent(
            tools=tool_schemas_for({"read_file"}),
            local_tools=[_review_tool(capture)],
            system_prompt=(
                "你是独立 Reviewer。检查任务覆盖、证据可定位性和报告冲突。若存在 evidence，必须先"
                "在一个 TAOR 轮用 read_file 回读其中至少一个路径，观察结果后再在后续轮提交审查。"
                "失败 Worker 必须被点名 revise；只有全部充分一致才 accept。"
            ),
            trace_metadata={
                "team_id": team_id,
                "parent_id": parent_id,
                "role": "reviewer",
                "review_round": round_number,
            },
            max_iterations=12,
            check_command=None,
            compact_executor="self",
        )
        capture.bind(agent)
        payload = {
            "goal": goal,
            "tasks": [asdict(task) for task in tasks],
            "worker_reports": [_worker_model(latest_reports[task.worker_id]) for task in tasks],
        }
        _team_log("🔎 [Reviewer]", f"开始第 {round_number} 次独立审查")
        detail, participant = self._run_agent(
            agent,
            "请审查以下材料并提交 ReviewReport：\n"
            + json.dumps(payload, ensure_ascii=False),
            role="reviewer",
            phase="review",
            attempt=round_number,
            parent_id=parent_id,
        )

        submitted = capture.report
        if detail.status != "succeeded" or submitted is None:
            reasons = []
            if detail.status != "succeeded":
                reasons.append(detail.error or f"Reviewer 状态={detail.status}")
            if submitted is None:
                reasons.append("未提交合法 ReviewReport")
            error = "；".join(reasons)
            participant.status = "failed"
            participant.error = error
            report = ReviewReport(
                verdict="failed",
                feedback="Reviewer 未完成有效审查：" + error,
                revise_worker_ids=[],
                trace_ref=detail.trace_ref,
                usage=dict(detail.usage),
                tool_calls=list(detail.tool_calls),
                round=round_number,
                error=error,
            )
        else:
            report = ReviewReport(
                verdict=submitted["verdict"],
                feedback=submitted["feedback"],
                revise_worker_ids=list(submitted["revise_worker_ids"]),
                trace_ref=detail.trace_ref,
                usage=dict(detail.usage),
                tool_calls=list(detail.tool_calls),
                round=round_number,
            )
        self.participant_runs.append(participant)
        self.review_history.append(report)
        _team_log("🧪 [Reviewer]", f"第 {round_number} 次 verdict={report.verdict}")
        return report

    def _write_manifest(self, result: TeamResult) -> None:
        if not result.trace_ref:
            return
        manifest_path = Path(result.trace_ref)
        manifest = {
            "team_id": result.team_id,
            "goal": self._goal,
            "status": result.status,
            "tasks": [asdict(task) for task in self._tasks],
            "worker_reports": [
                {
                    **_worker_model(report),
                    "attempt": report.attempt,
                    "trace_ref": report.trace_ref,
                }
                for report in self.worker_reports
            ],
            "review_history": [
                {
                    **_review_model(report),
                    "round": report.round,
                    "trace_ref": report.trace_ref,
                }
                for report in self.review_history
            ],
            "participant_runs": [
                {
                    "agent_id": run.agent_id,
                    "parent_id": run.parent_id,
                    "role": run.role,
                    "worker_id": run.worker_id,
                    "attempt": run.attempt,
                    "phase": run.phase,
                    "status": run.status,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "duration_seconds": run.duration_seconds,
                    "usage": run.usage,
                    "trace_ref": run.trace_ref,
                    "error": run.error,
                }
                for run in self.participant_runs
            ],
            "total_usage": result.total_usage,
        }
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            result.trace_ref = None
            _team_log("⚠️ [trace]", f"team.json 写入失败：{exc}", error=True)

    def _result(
        self,
        *,
        team_id: str,
        status: str,
        final_answer: str,
        review: ReviewReport | None,
        trace_root,
    ) -> TeamResult:
        trace_ref = str(Path(trace_root) / "team.json") if _trace_enabled() else None
        result = TeamResult(
            team_id=team_id,
            status=status,
            final_answer=final_answer,
            worker_reports=list(self.worker_reports),
            review=review,
            total_usage=sum_usage(run.usage for run in self.participant_runs),
            participant_runs=list(self.participant_runs),
            trace_ref=trace_ref,
        )
        self._write_manifest(result)
        return result

    def run(self, goal: str) -> TeamResult:
        """执行一次独立协作；同一 TeamCoordinator 明确拒绝并发复用。"""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("同一个 TeamCoordinator 不能并发运行")
        try:
            return self._run_once(goal)
        finally:
            self._run_lock.release()

    def _run_once(self, goal: str) -> TeamResult:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("团队目标不能为空")
        team_id = self._new_team_id()
        self.participant_runs = []
        self.worker_reports = []
        self.review_history = []
        self._goal = goal.strip()
        self._tasks = []
        self._agents = []
        self._active_workers = 0
        self._current_team_id = team_id
        now = datetime.now()
        self._trace_root = (
            Path(__file__).resolve().parent.parent.parent / "traces"
            / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}" / team_id
        )
        _team_log("🤝 [Team]", f"{team_id} 开始：{goal}")

        plan_capture = _PlanCapture()
        planner = self._new_agent(
            tools=[],
            local_tools=[_plan_tool(plan_capture)],
            system_prompt=(
                "你是多 Agent 团队的 Coordinator，当前只负责规划。把目标拆成 2～4 个彼此独立、"
                "可并行、只读且自包含的 WorkerTask，并要求文件路径/行号证据。必须调用 submit_plan。"
            ),
            trace_metadata={
                "team_id": team_id,
                "parent_id": None,
                "role": "coordinator",
                "phase": "planning",
            },
            max_iterations=8,
            check_command=None,
            compact_executor="self",
        )
        planner_id = getattr(planner, "trace_dir", Path("coordinator")).name
        trace_root = self._trace_root or getattr(planner, "trace_dir", Path("."))
        plan_detail, plan_run = self._run_agent(
            planner,
            "请为这个目标提交并行只读计划：\n" + goal,
            role="coordinator",
            phase="planning",
        )
        if plan_detail.status != "succeeded" or plan_capture.tasks is None:
            plan_run.status = "failed"
            plan_run.error = plan_detail.error or "Coordinator 未提交合法计划"
        self.participant_runs.append(plan_run)
        if plan_run.status == "failed":
            return self._result(
                team_id=team_id,
                status="failed",
                final_answer=f"[团队规划失败] {plan_run.error}",
                review=None,
                trace_root=trace_root,
            )

        tasks = plan_capture.tasks
        self._tasks = list(tasks)
        _team_log("📋 [Coordinator]", f"计划通过校验：{len(tasks)} 个 WorkerTask")
        latest = {
            report.worker_id: report
            for report in self._run_workers(team_id, planner_id, tasks, attempt=1)
        }

        review = self._run_review(team_id, planner_id, goal, tasks, latest, round_number=1)
        if review.error:
            return self._result(
                team_id=team_id,
                status="failed",
                final_answer=f"[团队审查失败] {review.error}",
                review=review,
                trace_root=trace_root,
            )

        if review.verdict == "revise":
            revise_set = set(review.revise_worker_ids)
            revise_tasks = [task for task in tasks if task.worker_id in revise_set]
            _team_log(
                "🔁 [Coordinator]",
                "只定向补充：" + ", ".join(task.worker_id for task in revise_tasks),
            )
            revised = self._run_workers(
                team_id,
                planner_id,
                revise_tasks,
                attempt=2,
                feedback=review.feedback,
                previous_reports=latest,
            )
            latest.update({report.worker_id: report for report in revised})
            review = self._run_review(team_id, planner_id, goal, tasks, latest, round_number=2)
            if review.error:
                return self._result(
                    team_id=team_id,
                    status="failed",
                    final_answer=f"[团队审查失败] {review.error}",
                    review=review,
                    trace_root=trace_root,
                )

        # 汇总用全新无工具 Agent，只接收精简业务 DTO，不重发规划历史或手改 Agent 私有字段。
        aggregator = self._new_agent(
            tools=[],
            system_prompt=(
                "你是最终汇总 Coordinator。只能依据 WorkerReports 和 Reviewer 结论写答案，不得编造。"
                "按 worker_id 引用文件证据；存在失败或最终 revise 时如实保留缺口。"
            ),
            trace_metadata={
                "team_id": team_id,
                "parent_id": planner_id,
                "role": "coordinator",
                "phase": "aggregation",
            },
            max_iterations=4,
            check_command=None,
            compact_executor="self",
        )
        payload = {
            "goal": goal,
            "latest_worker_reports": [_worker_model(latest[task.worker_id]) for task in tasks],
            "final_review": _review_model(review),
        }
        aggregate_detail, aggregate_run = self._run_agent(
            aggregator,
            "请汇总以下结构化材料：\n" + json.dumps(payload, ensure_ascii=False),
            role="coordinator",
            phase="aggregation",
            parent_id=planner_id,
        )
        self.participant_runs.append(aggregate_run)

        if aggregate_detail.status != "succeeded" or not aggregate_detail.output.strip():
            status = "failed"
            reason = aggregate_detail.error or aggregate_detail.output or "Coordinator 返回空汇总"
            final_answer = f"[团队汇总失败] {reason}"
        else:
            all_succeeded = all(report.status == "succeeded" for report in latest.values())
            status = "succeeded" if all_succeeded and review.verdict == "accept" else "completed_with_issues"
            final_answer = aggregate_detail.output

        result = self._result(
            team_id=team_id,
            status=status,
            final_answer=final_answer,
            review=review,
            trace_root=trace_root,
        )
        _team_log(
            "🏁 [Team]",
            f"{team_id} 结束：{result.status}，累计 out={result.total_usage['output_tokens']} token，"
            f"trace={result.trace_ref or 'off'}",
            error=result.status == "failed",
        )
        return result


def run_team(goal: str) -> TeamResult:
    """CLI /team 的便捷入口。"""
    return TeamCoordinator().run(goal)
