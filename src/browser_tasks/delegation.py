from __future__ import annotations

from .models import (
    DelegationRequest,
    DelegationResponse,
    DisclosureDecision,
    SCHEMA_VERSION,
    Task,
)


PURPOSES = {"plan", "review", "research", "synthesis"}


def validate_disclosure(decision: DisclosureDecision, request: DelegationRequest) -> None:
    if decision.schema_version != SCHEMA_VERSION or request.schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported schema version")
    if decision.status != "approved":
        raise ValueError("disclosure is not approved")
    if request.purpose not in PURPOSES:
        raise ValueError("unsupported delegation purpose")
    if (
        request.provider != "chatgpt-web"
        or request.transport != "surf-ui"
        or request.reasoning_effort not in {"best", "high", "max"}
        or request.research_mode not in {"standard", "deep"}
        or request.fallback_policy != "block"
    ):
        raise ValueError("delegation violates provider or transport policy")
    if (decision.task_id, decision.provider, decision.context_sha256) != (
        request.task_id, request.provider, request.context_sha256
    ):
        raise ValueError("disclosure does not match request")


def validate_response(request: DelegationRequest, response: DelegationResponse) -> None:
    if response.schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported response schema")
    if (response.task_id, response.request_id, response.provider, response.context_sha256) != (
        request.task_id, request.request_id, request.provider, request.context_sha256
    ):
        raise ValueError("delegation response does not match request")
    if response.kind != request.purpose or not isinstance(response.advice, dict):
        raise ValueError("invalid delegation response")


def validate_request_policy(task: Task, request: DelegationRequest) -> None:
    if request.task_id != task.task_id:
        raise ValueError("delegation belongs to another task")
    checks = {
        "policy": task.delegation_policy == "maximal",
        "provider": request.provider == task.delegate_provider == "chatgpt-web",
        "transport": request.transport == task.delegate_transport == "surf-ui",
        "reasoning": request.reasoning_effort in {"best", task.reasoning_effort},
        "research": (
            task.deep_research_policy == "auto"
            or request.research_mode == task.deep_research_policy
        ),
        "fallback": request.fallback_policy == task.fallback_policy == "block",
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise PermissionError(
            "delegation request violates task policy: " + ", ".join(failed)
        )
