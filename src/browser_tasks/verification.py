from __future__ import annotations

from .models import BrowserAction, BrowserObservation


def verify(action: BrowserAction, observation: BrowserObservation) -> str:
    if observation.task_id != action.task_id or observation.action_id != action.action_id:
        return "ambiguous"
    for requirement in action.postconditions:
        kind = requirement.get("type")
        value = requirement.get("value")
        if kind == "url_equals" and observation.url != value:
            return "failed"
        if kind == "state_equals":
            key = requirement.get("key")
            if not key or str(observation.state.get(key)) != value:
                return "failed"
        if kind not in {"url_equals", "state_equals"}:
            return "ambiguous"
    return "verified"
