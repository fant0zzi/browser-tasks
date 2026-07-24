from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import fcntl
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .lifecycle import transition
from .models import SCHEMA_VERSION, Task, TaskState
from .models import AuthorizationGrant, BrowserAction, ActionClass
from .authorization import validate_grant


TASK_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9][a-z0-9-]*$")
POLICIES = {"off", "suggest", "auto_readonly", "force", "maximal"}
BROWSER_POLICIES = {"user_browser_only"}
REASONING_EFFORTS = {"best", "high", "max"}
DEEP_RESEARCH_POLICIES = {"auto", "standard", "deep"}
FALLBACK_POLICIES = {"block"}
EXTERNAL_TOOL_POLICIES = {"surf_chatgpt_only"}


class TaskStore:
    def __init__(self, root: Path, active_task_id: str | None = None):
        self.root = root.resolve()
        self.tasks = self.root / "tasks"
        self.active_task_id = active_task_id
        if self.tasks.exists() and (self.tasks.is_symlink() or self.tasks.resolve().parent != self.root):
            raise ValueError("tasks root is not trusted")

    def bind(self, task_id: str) -> "TaskStore":
        self.path(task_id)
        return TaskStore(self.root, task_id)

    def _id(self, task_id: str | None = None) -> str:
        chosen = task_id or self.active_task_id
        if not chosen or not TASK_ID.fullmatch(chosen):
            raise ValueError("invalid or missing active task id")
        if self.active_task_id and chosen != self.active_task_id:
            raise ValueError("store is bound to another task")
        return chosen

    def path(self, task_id: str | None = None) -> Path:
        chosen = self._id(task_id)
        path = self.tasks / chosen
        if path.is_symlink():
            raise ValueError("task root must not be a symlink")
        if path.exists() and path.resolve().parent != self.tasks.resolve():
            raise ValueError("task escapes tasks root")
        return path

    def create(
        self,
        task_id: str,
        goal: str,
        constraints: tuple[str, ...] = (),
        *,
        delegation_policy: str = "maximal",
        allowed_browser_adapters: tuple[str, ...] = ("surf",),
        reasoning_effort: str = "best",
        deep_research_policy: str = "auto",
    ) -> Task:
        final = self.path(task_id)
        self.tasks.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{task_id}.", dir=self.tasks))
        os.chmod(stage, 0o700)
        try:
            for name in ("artifacts", "evidence", "delegations"):
                (stage / name).mkdir(mode=0o700)
            task = Task(
                task_id=task_id,
                goal=goal,
                constraints=constraints,
                delegation_policy=delegation_policy,
                allowed_browser_adapters=allowed_browser_adapters,
                reasoning_effort=reasoning_effort,
                deep_research_policy=deep_research_policy,
                created_at=datetime.now(UTC).isoformat(),
                state=TaskState.SCOPED,
            )
            self._write_json(stage / "task.json", task.to_dict())
            for name, text in (("request.md", f"# Request\n\n{goal}\n"), ("notes.md", "# Notes\n"),
                               ("result.md", "# Result\n\nWork in progress.\n"), ("events.jsonl", ""),
                               ("authorizations.json", "{}\n")):
                self._write_text(stage / name, text)
            os.rename(stage, final)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        self.bind(task_id).append_event("task.created", {"goal": goal})
        return task

    def load(self, task_id: str | None = None) -> Task:
        requested = self._id(task_id)
        target = self.path(requested) / "task.json"
        if target.is_symlink() or not target.is_file():
            raise ValueError("invalid task metadata")
        data = json.loads(target.read_text(encoding="utf-8"))
        required = {"schema_version", "task_id", "goal", "created_at", "state"}
        if not required.issubset(data) or data["schema_version"] != SCHEMA_VERSION:
            raise ValueError("invalid task schema")
        delegation_policy = data.get("delegation_policy", "maximal")
        browser_policy = data.get("browser_policy", "user_browser_only")
        reasoning_effort = data.get("reasoning_effort", "best")
        deep_research_policy = data.get("deep_research_policy", "auto")
        fallback_policy = data.get("fallback_policy", "block")
        external_tool_policy = data.get(
            "external_tool_policy", "surf_chatgpt_only"
        )
        delegate_provider = data.get("delegate_provider", "chatgpt-web")
        delegate_transport = data.get("delegate_transport", "surf-ui")
        allowed_adapters = tuple(data.get("allowed_browser_adapters", ["surf"]))
        if (
            data["task_id"] != requested
            or delegation_policy not in POLICIES
            or browser_policy not in BROWSER_POLICIES
            or reasoning_effort not in REASONING_EFFORTS
            or deep_research_policy not in DEEP_RESEARCH_POLICIES
            or fallback_policy not in FALLBACK_POLICIES
            or external_tool_policy not in EXTERNAL_TOOL_POLICIES
            or delegate_provider != "chatgpt-web"
            or delegate_transport != "surf-ui"
            or not allowed_adapters
            or any(item != "surf" for item in allowed_adapters)
        ):
            raise ValueError("task identity or policy mismatch")
        created = datetime.fromisoformat(data["created_at"])
        if created.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return Task(task_id=requested, goal=str(data["goal"]), created_at=data["created_at"],
                    state=TaskState(data["state"]), constraints=tuple(data.get("constraints", [])),
                    delegation_policy=delegation_policy,
                    authorization_policy=data.get("authorization_policy", "explicit"),
                    browser_policy=browser_policy,
                    allowed_browser_adapters=allowed_adapters,
                    delegate_provider=delegate_provider,
                    delegate_transport=delegate_transport,
                    reasoning_effort=reasoning_effort,
                    deep_research_policy=deep_research_policy,
                    fallback_policy=fallback_policy,
                    external_tool_policy=external_tool_policy,
                    active_browser_adapter=data.get("active_browser_adapter"),
                    owned_browser_resources=tuple(data.get("owned_browser_resources", [])))

    def enforce_delegate_first_policy(self) -> Task:
        with self._lock():
            task = self.load()
            changed = replace(
                task,
                delegation_policy="maximal",
                browser_policy="user_browser_only",
                allowed_browser_adapters=("surf",),
                delegate_provider="chatgpt-web",
                delegate_transport="surf-ui",
                reasoning_effort="best",
                deep_research_policy="auto",
                fallback_policy="block",
                external_tool_policy="surf_chatgpt_only",
            )
            self._write_json(self.path() / "task.json", changed.to_dict())
        self.append_event(
            "task.policy_enforced",
            {
                "delegation_policy": "maximal",
                "browser_adapter": "surf",
                "provider": "chatgpt-web",
                "transport": "surf-ui",
                "reasoning_effort": "best",
                "deep_research_policy": "auto",
                "fallback_policy": "block",
                "external_tool_policy": "surf_chatgpt_only",
            },
        )
        return changed

    def transition(self, expected: TaskState, target: TaskState, *, verified: bool = False) -> Task:
        with self._lock():
            task = self.load()
            if task.state is not expected:
                raise ValueError("stale task state")
            state = transition(task.state, target, verified=verified)
            changed = replace(task, state=state)
            self._write_json(self.path() / "task.json", changed.to_dict())
        self.append_event("task.state_changed", {"from": expected.value, "to": target.value, "verified": verified})
        return changed

    def bind_adapter(self, adapter_id: str, resources: tuple[str, ...]) -> Task:
        from .policy import ensure_browser_adapter_allowed

        if not resources or any(not item for item in resources):
            raise ValueError("adapter must claim resources")
        with self._lock():
            task = self.load()
            ensure_browser_adapter_allowed(task, adapter_id)
            if task.active_browser_adapter and task.active_browser_adapter != adapter_id:
                raise ValueError("adapter mismatch")
            changed = replace(task, active_browser_adapter=adapter_id, owned_browser_resources=tuple(resources))
            self._write_json(self.path() / "task.json", changed.to_dict())
        return changed

    def install_grant(self, grant: AuthorizationGrant) -> None:
        if grant.task_id != self._id():
            raise ValueError("grant belongs to another task")
        with self._lock():
            grants = self._load_grants()
            existing = grants.get(grant.grant_id)
            encoded = self._grant_dict(grant)
            if existing:
                persisted = self._grant_from_dict(existing)
                if replace(persisted, uses=grant.uses) != grant:
                    raise ValueError("grant id already exists with different authorization")
                return
            if not existing:
                grants[grant.grant_id] = encoded
                self._write_json(self.path() / "authorizations.json", grants)

    def reserve_execution(self, action: BrowserAction, grant_id: str | None) -> AuthorizationGrant | None:
        with self._lock():
            task = self.load()
            if task.state not in {TaskState.READY, TaskState.EXECUTING}:
                raise ValueError("task is not executable")
            if not task.active_browser_adapter or not task.owned_browser_resources:
                raise ValueError("browser adapter is not bound")
            used = None
            if grant_id:
                grants = self._load_grants()
                if grant_id not in grants:
                    raise PermissionError("grant is not installed")
                used = self._grant_from_dict(grants[grant_id])
                validate_grant(used, action)
                used = replace(used, uses=used.uses + 1)
                grants[grant_id] = self._grant_dict(used)
                self._write_json(self.path() / "authorizations.json", grants)
            if task.state is TaskState.READY:
                self._write_json(self.path() / "task.json", replace(task, state=TaskState.EXECUTING).to_dict())
            return used

    def append_event(self, event_type: str, payload: dict) -> None:
        task_id = self._id()
        event = {"schema_version": 1, "event_id": str(uuid4()), "task_id": task_id,
                 "timestamp": datetime.now(UTC).isoformat(), "actor": "harness",
                 "event_type": event_type, "payload": payload}
        target = self.path() / "events.jsonl"
        if target.exists() and target.is_symlink():
            raise ValueError("event log must not be a symlink")
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, (json.dumps(event, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _lock(self):
        class Lock:
            def __init__(self, target): self.target = target
            def __enter__(self):
                self.fd = os.open(self.target, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            def __exit__(self, *_):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
        return Lock(self.path() / ".lock")

    def _load_grants(self) -> dict:
        target = self.path() / "authorizations.json"
        if target.is_symlink():
            raise ValueError("authorization store must not be a symlink")
        return json.loads(target.read_text(encoding="utf-8"))

    @staticmethod
    def _grant_dict(grant: AuthorizationGrant) -> dict:
        data = grant.__dict__.copy()
        data["action_class"] = grant.action_class.value
        return data

    @staticmethod
    def _grant_from_dict(data: dict) -> AuthorizationGrant:
        return AuthorizationGrant(**{**data, "action_class": ActionClass(data["action_class"])})

    @staticmethod
    def _write_text(target: Path, text: str) -> None:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def _write_json(cls, target: Path, value: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.chmod(tmp, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
