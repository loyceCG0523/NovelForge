"""Agent 之间共享的输入、输出与失败协议。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


AGENT_PROTOCOL_VERSION = "agent_result.v1"
AGENT_INPUT_PROTOCOL_VERSION = "agent_input.v1"
AgentStatus = Literal["success", "degraded", "waiting", "failed"]


@dataclass(slots=True)
class AgentInputContract:
    """Worker 交给 Agent 的稳定输入信封。"""

    agent: str
    stage: str
    task_id: str
    novel_id: str
    payload: dict[str, Any]
    schema_version: str = AGENT_INPUT_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentFailure:
    """可持久化、可判断是否重试的失败信息。"""

    code: str
    message: str
    stage: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResultContract:
    """每个 Agent 输出中都可以附带的稳定元数据。"""

    agent: str
    stage: str
    status: AgentStatus = "success"
    generation_mode: str = ""
    degraded: bool = False
    failure: AgentFailure | None = None
    schema_version: str = AGENT_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.failure is None:
            payload["failure"] = {}
        return payload


class AgentExecutionError(RuntimeError):
    """携带稳定错误码和重试语义的 Agent 异常。"""

    def __init__(self, failure: AgentFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def agent_input_contract(
    agent: str,
    stage: str,
    *,
    task_id: str,
    novel_id: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return AgentInputContract(
        agent=agent,
        stage=stage,
        task_id=task_id,
        novel_id=novel_id,
        payload=payload or {},
    ).to_dict()


def agent_contract(
    agent: str,
    stage: str,
    *,
    status: AgentStatus = "success",
    generation_mode: str = "",
    degraded: bool = False,
    failure: AgentFailure | None = None,
) -> dict[str, Any]:
    return AgentResultContract(
        agent=agent,
        stage=stage,
        status=status,
        generation_mode=generation_mode,
        degraded=degraded,
        failure=failure,
    ).to_dict()
