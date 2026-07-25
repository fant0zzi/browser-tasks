from __future__ import annotations

from dataclasses import dataclass

from .adapters import BrowserAdapter
from .models import AuthorizationGrant, BrowserAction, BrowserObservation
from .policy import requires_authorization, retry_policy
from .task_store import TaskStore
from .verification import verify


@dataclass(frozen=True)
class ExecutionResult:
    outcome: str
    observation: BrowserObservation
    consumed_grant: AuthorizationGrant | None


class Orchestrator:
    """Drives one run's browser actions under that run's lease.

    The run id and lease owner are constructor arguments because every write
    the orchestrator performs is fenced by them; an orchestrator without them
    could act on behalf of a run it does not own.
    """

    def __init__(
        self,
        store: TaskStore,
        task_id: str,
        adapter: BrowserAdapter,
        *,
        run_id: str,
        lease_owner: str,
    ):
        self.store = store.bind(task_id)
        self.task_id = task_id
        self.adapter = adapter
        self.run_id = run_id
        self.lease_owner = lease_owner
        resources = adapter.claim(task_id)
        self.store.bind_adapter(adapter.adapter_id, resources)

    def execute(self, action: BrowserAction, grant: AuthorizationGrant | None = None) -> ExecutionResult:
        if action.task_id != self.task_id:
            raise ValueError("action belongs to another task")
        task = self.store.load()
        if task.active_browser_adapter != self.adapter.adapter_id:
            raise ValueError("adapter mismatch")
        authorized = requires_authorization(action.action_class)
        if authorized and grant is None:
            raise PermissionError("consequential action requires authorization")
        # Capture the pre-action state before the grant is consumed. Doing it
        # the other way round burned a single-use authorization whenever the
        # observation failed, leaving no action row to reconcile.
        pre = self.adapter.observe(self.task_id, action.target)
        self.store.append_event(
            "action.pre_state",
            {"action_id": action.action_id, "evidence": pre.evidence_sha256},
        )
        if authorized:
            self.store.install_grant(grant)
        # Reservation, authorization and the intent record share one
        # transaction, so the intent can never be attributed to a run that
        # replaced ours in between.
        used = self.store.reserve_execution(
            action,
            grant.grant_id if authorized else None,
            run_id=self.run_id,
            lease_owner=self.lease_owner,
        )
        if used is not None:
            self.store.append_event(
                "authorization.consumed",
                {"grant_id": used.grant_id, "action_id": action.action_id},
            )
        # Renew before a potentially long browser call so the lease outlives
        # the action it is fencing.
        self.store.heartbeat(self.run_id, lease_owner=self.lease_owner)
        try:
            observation = self.adapter.act(action)
            if observation.resource_id not in task.owned_browser_resources:
                raise ValueError(
                    "observation came from an unowned browser resource"
                )
            outcome = verify(action, observation)
        except Exception:
            failure_outcome = "ambiguous" if authorized else "failed"
            self.store.record_action_result(
                action.action_id,
                failure_outcome,
                run_id=self.run_id,
                lease_owner=self.lease_owner,
            )
            raise
        self.store.record_action_result(
            action.action_id,
            outcome,
            observation.evidence_sha256,
            run_id=self.run_id,
            lease_owner=self.lease_owner,
        )
        self.store.append_event(
            "action.retry_policy",
            {
                "action_id": action.action_id,
                "outcome": outcome,
                "retry_policy": retry_policy(action.action_class, outcome),
            },
        )
        return ExecutionResult(outcome, observation, used)
