from __future__ import annotations

from .models import BrowserAction, BrowserObservation
from .policy import requires_authorization


SUPPORTED_POSTCONDITIONS = {"url_equals", "state_equals"}


def postconditions_are_supported(
    postconditions: tuple[dict[str, str], ...]
) -> bool:
    """Every requirement must name a supported check with a concrete value."""

    for requirement in postconditions:
        kind = requirement.get("type")
        if kind not in SUPPORTED_POSTCONDITIONS:
            return False
        if requirement.get("value") is None:
            return False
        if kind == "state_equals" and not requirement.get("key"):
            return False
    return True


def verify(action: BrowserAction, observation: BrowserObservation) -> str:
    if observation.task_id != action.task_id or observation.action_id != action.action_id:
        return "ambiguous"
    if requires_authorization(action.action_class) and not action.postconditions:
        return "ambiguous"
    if not postconditions_are_supported(action.postconditions):
        return "ambiguous"
    for requirement in action.postconditions:
        kind = requirement["type"]
        value = requirement["value"]
        if kind == "url_equals" and observation.url != value:
            return "failed"
        if kind == "state_equals":
            if str(observation.state.get(requirement["key"])) != value:
                return "failed"
    return "verified"
