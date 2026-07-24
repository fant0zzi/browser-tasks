from __future__ import annotations

from .models import ActionClass, Task


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


def adapter_kind(adapter_id: str) -> str:
    return adapter_id.split(":", 1)[0]


def ensure_browser_adapter_allowed(task: Task, adapter_id: str) -> None:
    if task.browser_policy != "user_browser_only":
        raise ValueError("unsupported browser policy")
    if adapter_kind(adapter_id) not in task.allowed_browser_adapters:
        raise PermissionError(
            f"browser adapter is forbidden by task policy: {adapter_id}"
        )


EXTERNAL_CAPABILITY_TOOLS = {
    "browser": {"surf"},
    "reasoning": {"web-chat", "web-review"},
    "research": {"web-chat", "web-review"},
}


def ensure_external_tool_allowed(
    task: Task, capability: str, tool_name: str
) -> None:
    if task.external_tool_policy != "surf_chatgpt_only":
        raise ValueError("unsupported external tool policy")
    allowed = EXTERNAL_CAPABILITY_TOOLS.get(capability)
    if allowed is None:
        raise ValueError(f"unknown external capability: {capability}")
    if tool_name not in allowed:
        raise PermissionError(
            f"{tool_name} is forbidden for {capability}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )
