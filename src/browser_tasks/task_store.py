from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import Task, TaskState


TASK_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9][a-z0-9-]*$")


class TaskStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.tasks = self.root / "tasks"

    def path(self, task_id: str) -> Path:
        if not TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task id")
        path = self.tasks / task_id
        if path.is_symlink():
            raise ValueError("task root must not be a symlink")
        return path

    def create(self, task_id: str, goal: str, constraints: tuple[str, ...] = ()) -> Task:
        path = self.path(task_id)
        path.mkdir(parents=True, exist_ok=False)
        for name in ("artifacts", "evidence", "delegations"):
            (path / name).mkdir()
        task = Task(
            task_id=task_id,
            goal=goal,
            constraints=constraints,
            created_at=datetime.now(UTC).isoformat(),
            state=TaskState.SCOPED,
        )
        self._write_json(path / "task.json", task.to_dict())
        for name, text in (
            ("request.md", f"# Request\n\n{goal}\n"),
            ("notes.md", "# Notes\n"),
            ("result.md", "# Result\n\nWork in progress.\n"),
        ):
            (path / name).write_text(text, encoding="utf-8")
        (path / "events.jsonl").write_text("", encoding="utf-8")
        self.append_event(task_id, "task.created", {"goal": goal})
        return task

    def load(self, task_id: str) -> Task:
        path = self.path(task_id)
        data = json.loads((path / "task.json").read_text(encoding="utf-8"))
        return Task(
            task_id=data["task_id"],
            goal=data["goal"],
            created_at=data["created_at"],
            state=TaskState(data["state"]),
            constraints=tuple(data.get("constraints", [])),
            delegation_policy=data.get("delegation_policy", "suggest"),
            authorization_policy=data.get("authorization_policy", "explicit"),
            active_browser_adapter=data.get("active_browser_adapter"),
            owned_browser_resources=tuple(data.get("owned_browser_resources", [])),
        )

    def set_state(self, task: Task, state: TaskState) -> Task:
        changed = replace(task, state=state)
        self._write_json(self.path(task.task_id) / "task.json", changed.to_dict())
        self.append_event(task.task_id, "task.state_changed", {"from": task.state.value, "to": state.value})
        return changed

    def append_event(self, task_id: str, event_type: str, payload: dict) -> None:
        event = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "task_id": task_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "actor": "harness",
            "event_type": event_type,
            "payload": payload,
        }
        target = self.path(task_id) / "events.jsonl"
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(event, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)

    @staticmethod
    def _write_json(target: Path, value: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
