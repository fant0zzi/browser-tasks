from __future__ import annotations

from .models import TaskState


TERMINAL = {TaskState.COMPLETED, TaskState.CANCELLED, TaskState.FAILED}
TRANSITIONS = {
    TaskState.NEW: {TaskState.SCOPED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.SCOPED: {TaskState.PLANNED, TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.PLANNED: {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.READY: {TaskState.EXECUTING, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.EXECUTING: {TaskState.AWAITING_AUTHORIZATION, TaskState.VERIFYING, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.AWAITING_AUTHORIZATION: {TaskState.EXECUTING, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.VERIFYING: {TaskState.COMPLETED, TaskState.EXECUTING, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.BLOCKED: {TaskState.SCOPED, TaskState.PLANNED, TaskState.READY, TaskState.EXECUTING, TaskState.CANCELLED, TaskState.FAILED},
}


def transition(current: TaskState, target: TaskState, *, verified: bool = False) -> TaskState:
    if current in TERMINAL or target not in TRANSITIONS.get(current, set()):
        raise ValueError(f"illegal task transition: {current} -> {target}")
    if target is TaskState.COMPLETED and not verified:
        raise ValueError("COMPLETED requires verified=True")
    return target
