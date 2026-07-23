from __future__ import annotations

from dataclasses import dataclass

from .adapters import BrowserAdapter
from .authorization import consume
from .models import AuthorizationGrant, BrowserAction, BrowserObservation, TaskState
from .policy import requires_authorization, retry_policy
from .task_store import TaskStore
from .verification import verify


@dataclass(frozen=True)
class ExecutionResult:
    outcome: str
    observation: BrowserObservation
    consumed_grant: AuthorizationGrant | None


class Orchestrator:
    def __init__(self, store: TaskStore, task_id: str, adapter: BrowserAdapter):
        self.store = store.bind(task_id)
        self.task_id = task_id
        self.adapter = adapter

    def execute(self, action: BrowserAction, grant: AuthorizationGrant | None = None) -> ExecutionResult:
        if action.task_id != self.task_id:
            raise ValueError("action belongs to another task")
        task = self.store.load()
        if task.state not in {TaskState.READY, TaskState.EXECUTING}:
            raise ValueError("task is not executable")
        used = None
        if requires_authorization(action.action_class):
            if grant is None:
                raise PermissionError("consequential action requires authorization")
            used = consume(grant, action)
            self.store.append_event("authorization.consumed", {"grant_id": grant.grant_id, "action_id": action.action_id})
        pre = self.adapter.observe(self.task_id, action.target)
        self.store.append_event("action.pre_state", {"action_id": action.action_id, "evidence": pre.evidence_sha256})
        observation = self.adapter.act(action)
        outcome = verify(action, observation)
        self.store.append_event("action.result", {
            "action_id": action.action_id,
            "outcome": outcome,
            "evidence": observation.evidence_sha256,
            "retry_policy": retry_policy(action.action_class, outcome),
        })
        return ExecutionResult(outcome, observation, used)
