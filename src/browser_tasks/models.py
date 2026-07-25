from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = 1


class TaskState(StrEnum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class RunState(StrEnum):
    SCOPING = "SCOPING"
    PLANNING = "PLANNING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActionClass(StrEnum):
    OBSERVE = "observe"
    NAVIGATE = "navigate"
    PREPARE_MUTATION = "prepare_mutation"
    COMMIT_EXTERNAL = "commit_external"
    CREDENTIAL_OR_IDENTITY = "credential_or_identity"
    FINANCIAL = "financial"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class Task:
    task_id: str
    goal: str
    created_at: str
    state: TaskState = TaskState.DRAFT
    constraints: tuple[str, ...] = ()
    delegation_policy: str = "maximal"
    authorization_policy: str = "explicit"
    browser_policy: str = "user_browser_only"
    allowed_browser_adapters: tuple[str, ...] = ("surf",)
    delegate_provider: str = "chatgpt-web"
    delegate_transport: str = "surf-ui"
    reasoning_effort: str = "best"
    deep_research_policy: str = "auto"
    fallback_policy: str = "block"
    external_tool_policy: str = "surf_chatgpt_only"
    active_browser_adapter: str | None = None
    owned_browser_resources: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass(frozen=True)
class TaskRun:
    run_id: str
    task_id: str
    state: RunState
    created_at: str
    updated_at: str
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    resumes_run_id: str | None = None
    checkpoint: str | None = None
    failure: str | None = None
    failure_class: str | None = None
    recoverable: bool | None = None


@dataclass(frozen=True)
class Deliverable:
    name: str
    task_id: str
    path: str
    kind: str
    sha256: str
    revision: int
    produced_by_run: str | None
    reusable: bool
    verified: bool
    description: str
    entrypoint: str | None = None


@dataclass(frozen=True)
class AuthorizationGrant:
    grant_id: str
    task_id: str
    action_class: ActionClass
    target: str
    summary_sha256: str
    expires_at: str
    max_uses: int = 1
    uses: int = 0
    content_sha256: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class DisclosureDecision:
    decision_id: str
    task_id: str
    provider: str
    context_sha256: str
    included_roots: tuple[str, ...]
    status: str
    sensitivity_summary: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class RoutingInput:
    architecture: bool = False
    dependent_steps: int = 0
    ambiguity: bool = False
    safety_review: bool = False
    relevant_files: int = 0
    repeated_failures: bool = False
    substantial_final_review: bool = False
    deterministic: bool = False
    local_test_decides: bool = False
    live_observation_primary: bool = False
    sensitive_broad_context: bool = False
    web_research: bool = False
    current_information: bool = False
    cross_source_synthesis: bool = False
    regulatory: bool = False
    unfamiliar_domain: bool = False
    large_research_volume: bool = False
    deep_research_requested: bool = False
    deep_research_available: bool = True
    provider_available: bool = True
    transport_available: bool = True
    disclosure_authorized: bool = False
    user_forced: bool = False
    maximal_delegation: bool = True
    requested_provider: str = "chatgpt-web"
    requested_transport: str = "surf-ui"


@dataclass(frozen=True)
class RoutingDecision:
    decision: str
    score: int
    reasons: tuple[str, ...] = field(default_factory=tuple)
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    provider: str | None = None
    transport: str | None = None
    reasoning_effort: str = "best"
    research_mode: str = "none"
    fallback_policy: str = "block"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class BrowserAction:
    action_id: str
    task_id: str
    action_class: ActionClass
    target: str
    summary: str
    postconditions: tuple[dict[str, str], ...]
    content_sha256: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class BrowserObservation:
    task_id: str
    action_id: str | None
    url: str
    state: dict[str, Any]
    evidence_sha256: str
    resource_id: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class DelegationRequest:
    task_id: str
    request_id: str
    provider: str
    context_sha256: str
    purpose: str
    transport: str = "surf-ui"
    reasoning_effort: str = "best"
    research_mode: str = "standard"
    fallback_policy: str = "block"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class DelegationResponse:
    task_id: str
    request_id: str
    provider: str
    context_sha256: str
    kind: str
    advice: dict[str, Any]
    schema_version: int = SCHEMA_VERSION
