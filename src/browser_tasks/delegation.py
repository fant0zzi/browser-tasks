from __future__ import annotations

from .models import DelegationRequest, DelegationResponse, DisclosureDecision, SCHEMA_VERSION


def validate_disclosure(decision: DisclosureDecision, request: DelegationRequest) -> None:
    if decision.schema_version != SCHEMA_VERSION or request.schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported schema version")
    if decision.status != "approved":
        raise ValueError("disclosure is not approved")
    if request.purpose not in {"plan", "review"}:
        raise ValueError("unsupported delegation purpose")
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
