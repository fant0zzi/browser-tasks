from __future__ import annotations

from .models import ActionClass


CONSEQUENTIAL = {
    ActionClass.COMMIT_EXTERNAL,
    ActionClass.CREDENTIAL_OR_IDENTITY,
    ActionClass.FINANCIAL,
    ActionClass.DESTRUCTIVE,
}


def requires_authorization(action_class: ActionClass) -> bool:
    return action_class in CONSEQUENTIAL


def retry_policy(action_class: ActionClass, outcome: str) -> str:
    if action_class in CONSEQUENTIAL and outcome == "ambiguous":
        return "block"
    if action_class in CONSEQUENTIAL:
        return "observe_before_retry"
    return "retry_allowed"
