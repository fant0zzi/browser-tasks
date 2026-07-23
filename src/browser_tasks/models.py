from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = 1


class TaskState(StrEnum):
    NEW = "NEW"
    SCOPED = "SCOPED"
    PLANNED = "PLANNED"
    READY = "READY"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


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
    state: TaskState = TaskState.NEW
    constraints: tuple[str, ...] = ()
    delegation_policy: str = "suggest"
    authorization_policy: str = "explicit"
    active_browser_adapter: str | None = None
    owned_browser_resources: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


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
    provider_available: bool = True
    disclosure_authorized: bool = False
    user_forced: bool = False


@dataclass(frozen=True)
class RoutingDecision:
    decision: str
    score: int
    reasons: tuple[str, ...] = field(default_factory=tuple)
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION
