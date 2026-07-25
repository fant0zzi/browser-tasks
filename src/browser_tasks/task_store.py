from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .authorization import (
    summary_sha256,
    validate_evidence_digest,
    validate_grant,
)
from .policy import CONSEQUENTIAL, requires_authorization
from .models import (
    ActionClass,
    AuthorizationGrant,
    BrowserAction,
    Deliverable,
    RunState,
    Task,
    TaskRun,
    TaskState,
)


STORAGE_SCHEMA_VERSION = 1
WORKSPACE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$")
# A bare `20260725-093000` is as opaque as `20260725-093000-review`; both are
# rejected, and rejection applies on every read path, not only on creation.
TIMESTAMP_TASK_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(?:-[a-z0-9][a-z0-9-]*)?$")
DELIVERABLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GENERIC_SLUGS = {"task", "new-task", "browser-task", "browser-research", "research"}
# The delegation transport waits up to 2700s for a response, so a lease must
# outlive it by default; the ceiling keeps a dead worker from wedging the
# workspace forever.
DEFAULT_LEASE_SECONDS = 3600
MAX_LEASE_SECONDS = 21600
MIN_LEASE_SECONDS = 10
MAX_FREE_TEXT = 2000
PLACEHOLDER_SUMMARIES = {
    "work in progress",
    "in progress",
    "todo",
    "tbd",
    "pending",
    "done",
    "completed",
}
ACTIVE_RUN_STATES = {
    RunState.SCOPING,
    RunState.PLANNING,
    RunState.READY,
    RunState.EXECUTING,
    RunState.VERIFYING,
    RunState.WAITING,
}
TERMINAL_RUN_STATES = {
    RunState.SUCCEEDED,
    RunState.INTERRUPTED,
    RunState.FAILED,
    RunState.CANCELLED,
}


def _now() -> str:
    # Fixed microsecond precision keeps the stored timestamps lexicographically
    # ordered; `isoformat()` drops `.ffffff` on a whole second, and '+' sorts
    # before '.', which would invert two timestamps in the same second.
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _lease_expiry(lease_seconds: int) -> str:
    return _stamp(datetime.now(UTC) + timedelta(seconds=lease_seconds))


def validate_lease_seconds(lease_seconds: int) -> int:
    if lease_seconds < MIN_LEASE_SECONDS:
        raise ValueError(
            f"run lease must be at least {MIN_LEASE_SECONDS} seconds"
        )
    if lease_seconds > MAX_LEASE_SECONDS:
        raise ValueError(
            f"run lease must not exceed {MAX_LEASE_SECONDS} seconds"
        )
    return lease_seconds


def collapse_free_text(value: str, *, field: str) -> str:
    """Keep operator text on one line so it cannot forge README structure."""

    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > MAX_FREE_TEXT:
        raise ValueError(f"{field} must be at most {MAX_FREE_TEXT} characters")
    return cleaned


def uuid7() -> str:
    timestamp_ms = int(datetime.now(UTC).timestamp() * 1000)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


def normalize_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore")
    text = normalized.decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-+", "-", text)
    return text[:48].rstrip("-")


def normalize_search(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def validate_workspace_slug(slug: str) -> None:
    if (
        not WORKSPACE_SLUG.fullmatch(slug)
        or TIMESTAMP_TASK_ID.fullmatch(slug)
        or slug in GENERIC_SLUGS
    ):
        raise ValueError(
            "workspace slug must be a short semantic name such as "
            "'visa-slot-tracker'"
        )


def validate_outcome_summary(summary: str) -> str:
    cleaned = " ".join(summary.split())
    normalized = cleaned.casefold().rstrip(".!")
    if len(cleaned) < 8 or normalized in PLACEHOLDER_SUMMARIES:
        raise ValueError("outcome summary must be substantive, not a placeholder")
    return cleaned


class TaskStore:
    """Sparse storage for one durable user-intent workspace."""

    def __init__(self, root: Path, active_task_id: str | None = None):
        self.root = root.resolve()
        self.tasks = self.root / "tasks"
        self.active_task_id = active_task_id
        self._lock_depth = 0
        if self.tasks.exists() and (
            self.tasks.is_symlink() or self.tasks.resolve().parent != self.root
        ):
            raise ValueError("tasks root is not trusted")

    def bind(self, task_id: str) -> "TaskStore":
        path = self.path(task_id)
        if not path.is_dir():
            # A mistyped slug must say so instead of surfacing as invalid
            # workspace metadata.
            raise FileNotFoundError(f"no such workspace: {task_id}")
        return TaskStore(self.root, task_id)

    def _id(self, task_id: str | None = None) -> str:
        chosen = task_id or self.active_task_id
        if not chosen or not WORKSPACE_SLUG.fullmatch(chosen):
            raise ValueError("invalid or missing workspace slug")
        if TIMESTAMP_TASK_ID.fullmatch(chosen) or chosen in GENERIC_SLUGS:
            raise ValueError(
                "workspace slug must be a short semantic name such as "
                "'visa-slot-tracker'"
            )
        if self.active_task_id and chosen != self.active_task_id:
            raise ValueError("store is bound to another workspace")
        return chosen

    def path(self, task_id: str | None = None) -> Path:
        chosen = self._id(task_id)
        path = self.tasks / chosen
        if path.is_symlink():
            raise ValueError("workspace root must not be a symlink")
        if path.exists() and path.resolve().parent != self.tasks.resolve():
            raise ValueError("workspace escapes tasks root")
        return path

    def create(
        self,
        task_id: str,
        goal: str,
        constraints: tuple[str, ...] = (),
        *,
        title: str | None = None,
        idempotency_key: str | None = None,
        delegation_policy: str = "maximal",
        allowed_browser_adapters: tuple[str, ...] = ("surf",),
        reasoning_effort: str = "best",
        deep_research_policy: str = "auto",
    ) -> Task:
        validate_workspace_slug(task_id)
        goal = collapse_free_text(goal, field="workspace goal")
        final = self.path(task_id)
        if final.exists():
            if idempotency_key and self._existing_idempotency_key(task_id) == idempotency_key:
                return self.load(task_id)
            raise FileExistsError(f"workspace already exists: {task_id}")

        self.tasks.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._sweep_abandoned_stages()
        stage = Path(tempfile.mkdtemp(prefix=f".{task_id}.", dir=self.tasks))
        os.chmod(stage, 0o700)
        created_at = _now()
        display_title = (title or task_id.replace("-", " ").title()).strip()
        task = Task(
            task_id=task_id,
            goal=goal,
            constraints=constraints,
            delegation_policy=delegation_policy,
            allowed_browser_adapters=allowed_browser_adapters,
            reasoning_effort=reasoning_effort,
            deep_research_policy=deep_research_policy,
            created_at=created_at,
            state=TaskState.DRAFT,
        )
        try:
            private = stage / ".task"
            private.mkdir(mode=0o700)
            database = private / "state.sqlite"
            connection = self._connect(database)
            try:
                self._create_schema(connection)
                self._set_meta(connection, "task", task.to_dict())
                self._set_meta(connection, "internal_task_id", uuid7())
                self._set_meta(connection, "slug", task_id)
                self._set_meta(connection, "title", display_title)
                self._set_meta(connection, "created_at", created_at)
                self._set_meta(connection, "updated_at", created_at)
                self._set_meta(connection, "idempotency_key", idempotency_key)
                self._set_meta(connection, "outcome_summary", None)
                self._set_meta(connection, "activity_status", "idle")
                self._set_meta(connection, "current_run_id", None)
                self._set_meta(connection, "archived_at", None)
                self._set_meta(connection, "superseded_by", None)
                self._insert_event(
                    connection,
                    task_id,
                    "task.created",
                    {"goal": task.goal, "storage_schema": STORAGE_SCHEMA_VERSION},
                )
                self._set_meta(connection, "pending_publish", None)
                self._set_meta(connection, "pending_artifact", None)
                connection.commit()
            finally:
                connection.close()
            os.chmod(database, 0o600)
            self._write_text(
                stage / "README.md",
                self._readme_text(task, display_title, None, ()),
            )
            try:
                os.rename(stage, final)
            except OSError as error:
                # POSIX rename refuses a non-empty destination, so a second
                # creator that raced past the existence check lands here.
                if final.exists():
                    raise FileExistsError(
                        f"workspace already exists: {task_id}"
                    ) from error
                raise
            self._fsync_directory(self.tasks)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return task

    def _existing_idempotency_key(self, task_id: str) -> str | None:
        try:
            return self._meta(task_id).get("idempotency_key")
        except (OSError, ValueError, sqlite3.Error):
            return None

    def _sweep_abandoned_stages(self) -> None:
        """Remove staging directories left by a create that never renamed."""

        cutoff = datetime.now(UTC) - timedelta(hours=1)
        for candidate in self.tasks.glob(".*.*"):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
            except OSError:
                continue
            if modified < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)

    def load(self, task_id: str | None = None) -> Task:
        database = self._database(task_id)
        with self._read(database) as connection:
            task = self._load_task(connection)
        if task.task_id != self._id(task_id):
            raise ValueError("workspace identity mismatch")
        return task

    def info(self, task_id: str | None = None) -> dict:
        if task_id is not None and self.active_task_id is None:
            return self.bind(task_id).info()
        database = self._database(task_id)
        with self._read(database) as connection:
            metadata = self._all_meta(connection)
            runs = [asdict(item) for item in self._load_runs(connection)]
            deliverables = [
                asdict(item) for item in self._load_deliverables(connection)
            ]
            aliases = [
                row["alias"]
                for row in connection.execute(
                    "SELECT alias FROM aliases ORDER BY alias"
                )
            ]
            artifacts = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT artifact_id, run_id, category, path, sha256, created_at
                    FROM run_artifacts
                    ORDER BY created_at, artifact_id
                    """
                )
            ]
        metadata["task"] = self.load(task_id).to_dict()
        metadata["runs"] = runs
        metadata["deliverables"] = deliverables
        metadata["aliases"] = aliases
        metadata["artifacts"] = artifacts
        return metadata

    def audit_events(self) -> tuple[dict, ...]:
        with self._read(self._database()) as connection:
            return tuple(
                {
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "timestamp": row["timestamp"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in connection.execute(
                    """
                    SELECT sequence, event_id, timestamp, event_type, payload_json
                    FROM events
                    ORDER BY sequence
                    """
                )
            )

    def enforce_delegate_first_policy(self) -> Task:
        with self._transaction() as connection:
            task = self._load_task(connection)
            changed = replace(
                task,
                delegation_policy="maximal",
                browser_policy="user_browser_only",
                allowed_browser_adapters=("surf",),
                delegate_provider="chatgpt-web",
                delegate_transport="surf-ui",
                fallback_policy="block",
                external_tool_policy="surf_chatgpt_only",
            )
            self._set_meta(connection, "task", changed.to_dict())
            self._touch(connection)
            self._insert_event(
                connection,
                changed.task_id,
                "task.policy_enforced",
                {
                    "delegation_policy": "maximal",
                    "browser_adapter": "surf",
                    "provider": "chatgpt-web",
                    "transport": "surf-ui",
                },
            )
        self._render_readme()
        return changed

    def set_outcome(self, summary: str) -> None:
        summary = validate_outcome_summary(summary)
        with self._transaction() as connection:
            task = self._load_task(connection)
            metadata = self._all_meta(connection)
            if metadata.get("archived_at"):
                raise ValueError("restore the workspace before recording an outcome")
            if task.state in {TaskState.CANCELLED, TaskState.SUPERSEDED}:
                raise ValueError(
                    f"cannot record an outcome for task in state {task.state.value}"
                )
            self._set_meta(connection, "outcome_summary", summary)
            self._touch(connection)
            self._insert_event(
                connection,
                task.task_id,
                "task.outcome_updated",
                {"summary": summary},
            )
        self._render_readme()

    def complete(self, summary: str) -> Task:
        summary = validate_outcome_summary(summary)
        # Held across the consistency walk so a concurrent publish mid-swap
        # cannot make completion fail with a misleading "modified deliverable".
        with self._filesystem_lock(), self._transaction() as connection:
            task = self._load_task(connection)
            metadata = self._all_meta(connection)
            if task.state in {
                TaskState.CANCELLED,
                TaskState.SUPERSEDED,
            }:
                raise ValueError(f"cannot complete task in state {task.state.value}")
            if metadata.get("archived_at"):
                raise ValueError("restore the workspace before completing it")
            self._set_meta(connection, "outcome_summary", summary)
            self._validate_completion(connection)
            changed = replace(task, state=TaskState.COMPLETED)
            self._set_meta(connection, "task", changed.to_dict())
            self._set_meta(connection, "activity_status", "idle")
            self._set_meta(connection, "current_run_id", None)
            self._touch(connection)
            self._insert_event(
                connection,
                task.task_id,
                "task.completed",
                {"summary": summary, "verified": True},
            )
        # A completed workspace holds no browser resources; leaving the claim
        # would burn the tab for every other task forever.
        self._release_browser_resources()
        self._render_readme()
        return changed

    def archive(self) -> None:
        with self._transaction() as connection:
            task = self._load_task(connection)
            if connection.execute(
                "SELECT 1 FROM runs WHERE state IN "
                "('SCOPING','PLANNING','READY','EXECUTING','VERIFYING','WAITING')"
            ).fetchone():
                raise ValueError("cannot archive a workspace with an active run")
            archived_at = _now()
            self._set_meta(connection, "archived_at", archived_at)
            self._touch(connection)
            self._insert_event(
                connection,
                task.task_id,
                "task.archived",
                {"archived_at": archived_at},
            )
        self._release_browser_resources()
        self._render_readme()

    def restore(self) -> None:
        with self._transaction() as connection:
            task = self._load_task(connection)
            self._set_meta(connection, "archived_at", None)
            self._touch(connection)
            self._insert_event(connection, task.task_id, "task.restored", {})
        self._render_readme()

    def supersede(self, replacement_slug: str) -> Task:
        if replacement_slug == self._id():
            raise ValueError("a task cannot supersede itself")
        replacement_store = TaskStore(self.root, replacement_slug)
        replacement = replacement_store.load()
        replacement_meta = replacement_store._meta()
        if replacement.state in {TaskState.SUPERSEDED, TaskState.CANCELLED}:
            raise ValueError(
                f"replacement is in state {replacement.state.value}"
            )
        if replacement_meta.get("superseded_by") == self._id():
            raise ValueError("replacement chain would form a cycle")
        with self._transaction() as connection:
            task = self._load_task(connection)
            if connection.execute(
                "SELECT 1 FROM runs WHERE state IN "
                "('SCOPING','PLANNING','READY','EXECUTING','VERIFYING','WAITING')"
            ).fetchone():
                raise ValueError("cannot supersede a workspace with an active run")
            changed = replace(task, state=TaskState.SUPERSEDED)
            self._set_meta(connection, "task", changed.to_dict())
            self._set_meta(connection, "superseded_by", replacement.task_id)
            self._touch(connection)
            self._insert_event(
                connection,
                task.task_id,
                "task.superseded",
                {"replacement": replacement.task_id},
            )
        self._render_readme()
        return changed

    def cancel(self, reason: str) -> Task:
        reason = collapse_free_text(reason, field="cancellation reason")
        with self._transaction() as connection:
            task = self._load_task(connection)
            if task.state in {TaskState.COMPLETED, TaskState.SUPERSEDED}:
                raise ValueError(f"cannot cancel task in state {task.state.value}")
            if connection.execute(
                "SELECT 1 FROM runs WHERE state IN "
                "('SCOPING','PLANNING','READY','EXECUTING','VERIFYING','WAITING')"
            ).fetchone():
                raise ValueError("cannot cancel a workspace with an active run")
            changed = replace(task, state=TaskState.CANCELLED)
            self._set_meta(connection, "task", changed.to_dict())
            self._set_meta(connection, "activity_status", "idle")
            self._set_meta(connection, "current_run_id", None)
            self._touch(connection)
            self._insert_event(
                connection,
                task.task_id,
                "task.cancelled",
                {"reason": reason},
            )
        self._release_browser_resources()
        self._render_readme()
        return changed

    def bind_adapter(self, adapter_id: str, resources: tuple[str, ...]) -> Task:
        from .policy import ensure_browser_adapter_allowed

        if not resources or any(not item for item in resources):
            raise ValueError("adapter must claim resources")
        # Reject a forbidden adapter before it can claim anything.
        ensure_browser_adapter_allowed(self.load(), adapter_id)
        self._claim_browser_resources(adapter_id, tuple(resources))
        with self._transaction() as connection:
            task = self._load_task(connection)
            ensure_browser_adapter_allowed(task, adapter_id)
            if task.active_browser_adapter and task.active_browser_adapter != adapter_id:
                raise ValueError("adapter mismatch")
            changed = replace(
                task,
                active_browser_adapter=adapter_id,
                owned_browser_resources=tuple(resources),
            )
            self._set_meta(connection, "task", changed.to_dict())
            self._touch(connection)
            self._insert_event(
                connection,
                task.task_id,
                "browser.adapter_bound",
                {"adapter_id": adapter_id, "resources": list(resources)},
            )
        return changed

    def _resource_registry_path(self) -> Path:
        return self.tasks / ".browser-resources.json"

    def _claim_browser_resources(
        self, adapter_id: str, resources: tuple[str, ...]
    ) -> None:
        """Tabs and windows are task-owned across the whole tasks root.

        The per-task metadata cannot see another workspace's claims, so the
        registry lives beside the workspaces and is updated under a lock.
        """

        task_id = self._id()
        self.tasks.mkdir(mode=0o700, parents=True, exist_ok=True)
        registry = self._resource_registry_path()
        if registry.is_symlink():
            raise ValueError("browser resource registry is unsafe")
        fd = os.open(registry, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 1 << 20).decode("utf-8") or "{}"
            try:
                claims = json.loads(raw)
            except json.JSONDecodeError:
                claims = {}
            for resource in resources:
                holder = claims.get(resource)
                if holder and holder.get("task_id") != task_id:
                    raise PermissionError(
                        f"browser resource {resource} is owned by "
                        f"{holder['task_id']}"
                    )
            live = {
                key: value
                for key, value in claims.items()
                if value.get("task_id") != task_id
            }
            for resource in resources:
                live[resource] = {"task_id": task_id, "adapter_id": adapter_id}
            payload = json.dumps(live, indent=2, sort_keys=True).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.truncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _release_browser_resources(self) -> tuple[str, ...]:
        registry = self._resource_registry_path()
        if not registry.is_file() or registry.is_symlink():
            return ()
        task_id = self._id()
        released: tuple[str, ...] = ()
        fd = os.open(registry, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 1 << 20).decode("utf-8") or "{}"
            try:
                claims = json.loads(raw)
            except json.JSONDecodeError:
                return ()
            released = tuple(
                sorted(
                    key
                    for key, value in claims.items()
                    if value.get("task_id") == task_id
                )
            )
            live = {
                key: value
                for key, value in claims.items()
                if value.get("task_id") != task_id
            }
            payload = json.dumps(live, indent=2, sort_keys=True).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.truncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return released

    def install_grant(self, grant: AuthorizationGrant) -> None:
        if grant.task_id != self._id():
            raise ValueError("grant belongs to another task")
        encoded = self._grant_dict(grant)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT data_json FROM authorizations WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()
            if row:
                persisted = self._grant_from_dict(json.loads(row["data_json"]))
                if replace(persisted, uses=grant.uses) != grant:
                    raise ValueError(
                        "grant id already exists with different authorization"
                    )
                return
            connection.execute(
                "INSERT INTO authorizations(grant_id, data_json) VALUES (?, ?)",
                (grant.grant_id, json.dumps(encoded, sort_keys=True)),
            )
            self._insert_event(
                connection,
                grant.task_id,
                "authorization.installed",
                {
                    "grant_id": grant.grant_id,
                    "action_class": grant.action_class.value,
                    "target": grant.target,
                    "max_uses": grant.max_uses,
                    "expires_at": grant.expires_at,
                },
            )
            self._touch(connection)

    def _require_owned_run(
        self,
        connection: sqlite3.Connection,
        metadata: dict,
        *,
        run_id: str,
        lease_owner: str,
        now: str,
    ) -> None:
        """The caller must hold the workspace's current, unexpired lease.

        Enforced here rather than only in heartbeat/finish because this is the
        gate a consequential browser action passes through.
        """

        if not run_id or not lease_owner:
            raise ValueError("execution requires an owned run and lease owner")
        current_run_id = metadata.get("current_run_id")
        if current_run_id != run_id:
            raise PermissionError(
                "execution run is not the workspace's current run"
            )
        row = connection.execute(
            "SELECT state, lease_owner, lease_expires_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row or RunState(row["state"]) not in ACTIVE_RUN_STATES:
            raise ValueError("browser execution requires an active run")
        if row["lease_owner"] != lease_owner:
            raise PermissionError("run lease belongs to another worker")
        if not row["lease_expires_at"] or row["lease_expires_at"] < now:
            raise ValueError("run lease has expired")

    def reserve_execution(
        self,
        action: BrowserAction,
        grant_id: str | None,
        *,
        run_id: str,
        lease_owner: str,
    ) -> AuthorizationGrant | None:
        """Fence, authorize and record the intent in a single transaction."""

        if action.task_id != self._id():
            raise ValueError("action belongs to another task")
        if requires_authorization(action.action_class) and not grant_id:
            raise PermissionError(
                "consequential action requires an authorization grant"
            )
        self._validate_action_contract(action)
        now = _now()
        with self._transaction() as connection:
            task = self._load_task(connection)
            metadata = self._all_meta(connection)
            if task.state in {TaskState.CANCELLED, TaskState.SUPERSEDED}:
                raise ValueError("task is not executable")
            if metadata.get("archived_at"):
                raise ValueError("restore the workspace before executing actions")
            self._require_owned_run(
                connection,
                metadata,
                run_id=run_id,
                lease_owner=lease_owner,
                now=now,
            )
            if not task.active_browser_adapter or not task.owned_browser_resources:
                raise ValueError("browser adapter is not bound")
            self._reject_unresolved_duplicate(connection, action)
            used = None
            if grant_id:
                row = connection.execute(
                    "SELECT data_json FROM authorizations WHERE grant_id = ?",
                    (grant_id,),
                ).fetchone()
                if not row:
                    raise PermissionError("grant is not installed")
                used = self._grant_from_dict(json.loads(row["data_json"]))
                validate_grant(used, action)
                used = replace(used, uses=used.uses + 1)
                connection.execute(
                    "UPDATE authorizations SET data_json = ? WHERE grant_id = ?",
                    (json.dumps(self._grant_dict(used), sort_keys=True), grant_id),
                )
            self._insert_action_intent(connection, action, run_id, now)
            self._touch(connection)
            return used

    @staticmethod
    def _validate_action_contract(action: BrowserAction) -> None:
        from .verification import postconditions_are_supported

        if not postconditions_are_supported(action.postconditions):
            raise ValueError(
                "action postconditions must name supported checks with values"
            )
        if requires_authorization(action.action_class) and not action.postconditions:
            raise ValueError(
                "a consequential action requires at least one postcondition"
            )

    @staticmethod
    def _reject_unresolved_duplicate(
        connection: sqlite3.Connection, action: BrowserAction
    ) -> None:
        """Refuse a semantically identical retry while the first is unresolved.

        A new action id must not become a second external submission of the
        same intent; the earlier attempt has to be reconciled first.
        """

        if not requires_authorization(action.action_class):
            return
        row = connection.execute(
            """
            SELECT action_id, status FROM actions
            WHERE action_class = ?
              AND target = ?
              AND summary_sha256 = ?
              AND IFNULL(content_sha256, '') = IFNULL(?, '')
              AND status IN ('INTENDED', 'OUTCOME_UNKNOWN')
              AND action_id != ?
            LIMIT 1
            """,
            (
                action.action_class.value,
                action.target,
                summary_sha256(action),
                action.content_sha256,
                action.action_id,
            ),
        ).fetchone()
        if row:
            raise ValueError(
                "an equivalent consequential action is unresolved: "
                f"{row['action_id']} ({row['status']}); reconcile it first"
            )

    @staticmethod
    def _insert_action_intent(
        connection: sqlite3.Connection,
        action: BrowserAction,
        run_id: str,
        now: str,
    ) -> None:
        existing = connection.execute(
            "SELECT status FROM actions WHERE action_id = ?",
            (action.action_id,),
        ).fetchone()
        if existing:
            raise ValueError(
                f"action intent already exists with status {existing['status']}"
            )
        connection.execute(
            """
            INSERT INTO actions(
                action_id, run_id, action_class, target, summary_sha256,
                content_sha256, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'INTENDED', ?, ?)
            """,
            (
                action.action_id,
                run_id,
                action.action_class.value,
                action.target,
                summary_sha256(action),
                action.content_sha256,
                now,
                now,
            ),
        )
        TaskStore._insert_event(
            connection,
            action.task_id,
            "action.intent",
            {
                "action_id": action.action_id,
                "action_class": action.action_class.value,
                "target": action.target,
                "run_id": run_id,
            },
        )

    def record_action_intent(
        self, action: BrowserAction, *, run_id: str, lease_owner: str
    ) -> None:
        """Record an intent for a read-only action.

        A consequential action must go through `reserve_execution`, which is the
        only path that validates and consumes an authorization grant. Allowing
        it here would make the recorded, documented sequence the unauthorized
        one.
        """

        if action.task_id != self._id():
            raise ValueError("action belongs to another task")
        if requires_authorization(action.action_class):
            raise PermissionError(
                f"{action.action_class.value} requires an authorization grant; "
                "reserve execution with a grant instead of recording a bare intent"
            )
        self._validate_action_contract(action)
        now = _now()
        with self._transaction() as connection:
            metadata = self._all_meta(connection)
            self._require_owned_run(
                connection,
                metadata,
                run_id=run_id,
                lease_owner=lease_owner,
                now=now,
            )
            self._reject_unresolved_duplicate(connection, action)
            self._insert_action_intent(connection, action, run_id, now)
            self._touch(connection)

    def record_action_result(
        self,
        action_id: str,
        outcome: str,
        evidence_sha256: str | None = None,
        *,
        run_id: str,
        lease_owner: str,
    ) -> str:
        now = _now()
        if evidence_sha256 is not None:
            evidence_sha256 = validate_evidence_digest(evidence_sha256)
        with self._transaction() as connection:
            task = self._load_task(connection)
            metadata = self._all_meta(connection)
            self._require_owned_run(
                connection,
                metadata,
                run_id=run_id,
                lease_owner=lease_owner,
                now=now,
            )
            row = connection.execute(
                "SELECT action_class, status, run_id FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not row:
                raise ValueError("action intent does not exist")
            if row["status"] != "INTENDED":
                raise ValueError("action already has a terminal outcome")
            if row["run_id"] != run_id:
                raise PermissionError("action belongs to another run")
            action_class = ActionClass(row["action_class"])
            consequential = action_class in CONSEQUENTIAL
            if outcome == "verified":
                if consequential and not evidence_sha256:
                    raise ValueError(
                        "a verified consequential action requires observed evidence"
                    )
                if consequential:
                    # Same anchoring as reconciliation: a well-formed digest that
                    # matches nothing captured is still a fabricated result.
                    self._require_stored_evidence(connection, evidence_sha256)
                status = "VERIFIED"
            elif outcome == "ambiguous" and consequential:
                status = "OUTCOME_UNKNOWN"
            else:
                status = "FAILED"
            connection.execute(
                """
                UPDATE actions
                SET status = ?, outcome = ?, evidence_sha256 = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (status, outcome, evidence_sha256, _now(), action_id),
            )
            self._insert_event(
                connection,
                task.task_id,
                "action.result",
                {
                    "action_id": action_id,
                    "outcome": outcome,
                    "status": status,
                    "evidence": evidence_sha256,
                },
            )
            self._touch(connection)
            return status

    def unresolved_actions(self) -> tuple[dict, ...]:
        with self._read(self._database()) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT action_id, run_id, action_class, target, status,
                           created_at, updated_at
                    FROM actions
                    WHERE status IN ('INTENDED', 'OUTCOME_UNKNOWN')
                    ORDER BY created_at, action_id
                    """
                )
            )

    def reconcile_action(
        self,
        action_id: str,
        *,
        verified: bool,
        evidence_sha256: str,
    ) -> None:
        evidence_sha256 = validate_evidence_digest(evidence_sha256)
        with self._transaction() as connection:
            task = self._load_task(connection)
            row = connection.execute(
                "SELECT status FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not row or row["status"] not in {"INTENDED", "OUTCOME_UNKNOWN"}:
                raise ValueError("action is not awaiting reconciliation")
            self._require_stored_evidence(connection, evidence_sha256)
            status = "VERIFIED" if verified else "FAILED"
            outcome = "reconciled_verified" if verified else "reconciled_failed"
            connection.execute(
                """
                UPDATE actions
                SET status = ?, outcome = ?, evidence_sha256 = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (status, outcome, evidence_sha256, _now(), action_id),
            )
            self._insert_event(
                connection,
                task.task_id,
                "action.reconciled",
                {
                    "action_id": action_id,
                    "status": status,
                    "evidence": evidence_sha256,
                },
            )
            self._touch(connection)

    @staticmethod
    def _require_stored_evidence(
        connection: sqlite3.Connection, evidence_sha256: str
    ) -> None:
        """Evidence must be the digest of something actually captured.

        A well-formed digest that matches no stored artifact is indistinguishable
        from free text as far as the audit trail is concerned.
        """

        anchored = connection.execute(
            """
            SELECT 1 FROM run_artifacts
            WHERE sha256 = ? AND category IN ('evidence', 'receipts')
            LIMIT 1
            """,
            (evidence_sha256,),
        ).fetchone()
        if not anchored:
            raise ValueError(
                "evidence must match a stored evidence or receipt artifact"
            )

    def append_event(self, event_type: str, payload: dict) -> None:
        with self._transaction() as connection:
            task = self._load_task(connection)
            self._insert_event(connection, task.task_id, event_type, payload)
            self._touch(connection)

    def start_run(
        self,
        state: RunState = RunState.EXECUTING,
        *,
        lease_owner: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        resumes_run_id: str | None = None,
    ) -> TaskRun:
        if state not in ACTIVE_RUN_STATES:
            raise ValueError("a run must start in an active state")
        validate_lease_seconds(lease_seconds)
        # Refuse an unusable workspace before recovery mutates anything, so a
        # rejected call has no side effects.
        self._require_runnable_workspace()
        self.recover_expired_runs()
        created_at = _now()
        expiry = _lease_expiry(lease_seconds)
        run = TaskRun(
            run_id=uuid7(),
            task_id=self._id(),
            state=state,
            created_at=created_at,
            updated_at=created_at,
            lease_owner=lease_owner or f"pid:{os.getpid()}",
            lease_expires_at=expiry,
            heartbeat_at=created_at,
            resumes_run_id=resumes_run_id,
        )
        with self._transaction() as connection:
            task = self._load_task(connection)
            metadata = self._all_meta(connection)
            if metadata.get("archived_at"):
                raise ValueError("restore the workspace before starting a run")
            if task.state in {TaskState.CANCELLED, TaskState.SUPERSEDED}:
                raise ValueError(
                    f"cannot start a run for task in state {task.state.value}"
                )
            active = connection.execute(
                "SELECT run_id FROM runs WHERE state IN "
                "('SCOPING','PLANNING','READY','EXECUTING','VERIFYING','WAITING')"
            ).fetchone()
            if active:
                raise ValueError(f"workspace already has an active run: {active['run_id']}")
            if resumes_run_id:
                previous = connection.execute(
                    "SELECT state FROM runs WHERE run_id = ?", (resumes_run_id,)
                ).fetchone()
                if not previous or RunState(previous["state"]) not in TERMINAL_RUN_STATES:
                    raise ValueError("resumed run must reference a terminal run")
            if task.state is not TaskState.COMPLETED:
                self._set_meta(
                    connection,
                    "task",
                    replace(task, state=TaskState.OPEN).to_dict(),
                )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, state, created_at, updated_at, lease_owner,
                    lease_expires_at, heartbeat_at, resumes_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.state.value,
                    run.created_at,
                    run.updated_at,
                    run.lease_owner,
                    run.lease_expires_at,
                    run.heartbeat_at,
                    run.resumes_run_id,
                ),
            )
            self._set_meta(connection, "activity_status", "running")
            self._set_meta(connection, "current_run_id", run.run_id)
            self._touch(connection)
            self._insert_event(
                connection,
                run.task_id,
                "run.started",
                {"run_id": run.run_id, "state": run.state.value},
            )
        self._render_readme()
        return run

    def _require_runnable_workspace(self) -> None:
        with self._read(self._database()) as connection:
            task = self._load_task(connection)
            metadata = self._all_meta(connection)
        if metadata.get("archived_at"):
            raise ValueError("restore the workspace before starting a run")
        if task.state in {TaskState.CANCELLED, TaskState.SUPERSEDED}:
            raise ValueError(
                f"cannot start a run for task in state {task.state.value}"
            )

    def heartbeat(
        self,
        run_id: str,
        *,
        lease_owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> TaskRun:
        validate_lease_seconds(lease_seconds)
        now = _now()
        expiry = _lease_expiry(lease_seconds)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row or RunState(row["state"]) not in ACTIVE_RUN_STATES:
                raise ValueError("run is not active")
            if row["lease_owner"] != lease_owner:
                raise PermissionError("run lease belongs to another worker")
            expired = bool(
                row["lease_expires_at"] and row["lease_expires_at"] < now
            )
            connection.execute(
                """
                UPDATE runs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (now, expiry, now, run_id),
            )
            if expired:
                # The run was never recovered, so the returning owner is still
                # the only writer; renewing beats wedging the workspace.
                self._insert_event(
                    connection,
                    self._id(),
                    "run.lease_renewed_after_expiry",
                    {"run_id": run_id, "lease_owner": lease_owner},
                )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def set_run_state(
        self, run_id: str, state: RunState, *, lease_owner: str
    ) -> TaskRun:
        """Advance a run between active states without ending it."""

        if state not in ACTIVE_RUN_STATES:
            raise ValueError("use run-finish to reach a terminal state")
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, lease_owner FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row or RunState(row["state"]) not in ACTIVE_RUN_STATES:
                raise ValueError("run is not active")
            if row["lease_owner"] != lease_owner:
                raise PermissionError("run lease belongs to another worker")
            previous = RunState(row["state"])
            connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (state.value, now, run_id),
            )
            self._insert_event(
                connection,
                self._id(),
                "run.state_changed",
                {
                    "run_id": run_id,
                    "from": previous.value,
                    "to": state.value,
                },
            )
            self._touch(connection)
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def abandon_run(self, run_id: str, reason: str) -> TaskRun:
        """Operator takeover for a run whose owner will never return."""

        reason = collapse_free_text(reason, field="abandon reason")
        now = _now()
        with self._transaction() as connection:
            task = self._load_task(connection)
            row = connection.execute(
                "SELECT state, lease_owner FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row or RunState(row["state"]) not in ACTIVE_RUN_STATES:
                raise ValueError("run is not active")
            connection.execute(
                """
                UPDATE runs
                SET state = 'INTERRUPTED', updated_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, failure = ?,
                    failure_class = 'worker_abandoned', recoverable = 1
                WHERE run_id = ?
                """,
                (now, reason, run_id),
            )
            self._fail_open_intents(connection, run_id, now, reason)
            self._set_meta(connection, "activity_status", "paused")
            self._set_meta(connection, "current_run_id", None)
            if task.state not in {TaskState.COMPLETED, TaskState.SUPERSEDED}:
                self._set_meta(
                    connection,
                    "task",
                    replace(task, state=TaskState.PAUSED).to_dict(),
                )
            self._insert_event(
                connection,
                task.task_id,
                "run.abandoned",
                {
                    "run_id": run_id,
                    "previous_owner": row["lease_owner"],
                    "reason": reason,
                },
            )
            self._touch(connection)
            result = self._run_from_row(
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )
        self._discard_scratch(run_id)
        self._render_readme()
        return result

    @staticmethod
    def _fail_open_intents(
        connection: sqlite3.Connection, run_id: str, now: str, reason: str
    ) -> None:
        """Close the run's open intents so none can outlive it as INTENDED."""

        connection.execute(
            """
            UPDATE actions
            SET status = CASE
                    WHEN action_class IN (
                        'commit_external',
                        'credential_or_identity',
                        'financial',
                        'destructive'
                    ) THEN 'OUTCOME_UNKNOWN'
                    ELSE 'FAILED'
                END,
                outcome = ?,
                updated_at = ?
            WHERE run_id = ? AND status = 'INTENDED'
            """,
            (reason, now, run_id),
        )

    def finish_run(
        self,
        run_id: str,
        state: RunState,
        *,
        lease_owner: str,
        checkpoint: str | None = None,
        failure: str | None = None,
        failure_class: str | None = None,
        recoverable: bool | None = None,
    ) -> TaskRun:
        if state not in TERMINAL_RUN_STATES:
            raise ValueError("run must finish in a terminal state")
        if state is RunState.FAILED and (
            not (failure or "").strip()
            or not (failure_class or "").strip()
            or recoverable is None
        ):
            raise ValueError(
                "failed run requires explanation, failure class, and "
                "recoverability"
            )
        if state is RunState.SUCCEEDED and any(
            item is not None for item in (failure, failure_class, recoverable)
        ):
            raise ValueError("successful run cannot record a failure")
        now = _now()
        with self._transaction() as connection:
            task = self._load_task(connection)
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row or RunState(row["state"]) not in ACTIVE_RUN_STATES:
                raise ValueError("run is not active")
            if row["lease_owner"] != lease_owner:
                raise PermissionError("run lease belongs to another worker")
            if row["lease_expires_at"] and row["lease_expires_at"] < now:
                # Nobody recovered the run, so the owner is still the only
                # writer and must be able to record what actually happened
                # instead of having a finished run rewritten as interrupted.
                self._insert_event(
                    connection,
                    task.task_id,
                    "run.finished_after_lease_expiry",
                    {"run_id": run_id, "lease_owner": lease_owner},
                )
            connection.execute(
                """
                UPDATE runs
                SET state = ?, updated_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, checkpoint = ?, failure = ?,
                    failure_class = ?, recoverable = ?
                WHERE run_id = ?
                """,
                (
                    state.value,
                    now,
                    checkpoint,
                    failure,
                    failure_class,
                    None if recoverable is None else int(recoverable),
                    run_id,
                ),
            )
            self._fail_open_intents(
                connection,
                run_id,
                now,
                f"run finished as {state.value} before the outcome was recorded",
            )
            activity = {
                RunState.INTERRUPTED: "paused",
                RunState.FAILED: "failed",
            }.get(state, "idle")
            self._set_meta(connection, "activity_status", activity)
            self._set_meta(connection, "current_run_id", None)
            task_terminal_state = {
                RunState.INTERRUPTED: TaskState.PAUSED,
                RunState.CANCELLED: TaskState.PAUSED,
                RunState.FAILED: TaskState.FAILED,
            }.get(state)
            if task_terminal_state and task.state not in {
                TaskState.COMPLETED,
                TaskState.SUPERSEDED,
            }:
                self._set_meta(
                    connection,
                    "task",
                    replace(task, state=task_terminal_state).to_dict(),
                )
            self._touch(connection)
            self._insert_event(
                connection,
                task.task_id,
                "run.finished",
                {
                    "run_id": run_id,
                    "state": state.value,
                    "checkpoint": checkpoint,
                    "failure": failure,
                    "failure_class": failure_class,
                    "recoverable": recoverable,
                },
            )
            result = self._run_from_row(
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )
        self._discard_scratch(run_id)
        self._release_browser_resources()
        self._render_readme()
        return result

    def release_browser_resources(self) -> tuple[str, ...]:
        """Give up this workspace's browser resource claims."""

        released = self._release_browser_resources()
        if released:
            with self._transaction() as connection:
                self._insert_event(
                    connection,
                    self._id(),
                    "browser.resources_released",
                    {"resources": list(released)},
                )
                self._touch(connection)
        return released

    def recover_expired_runs(self, *, now: datetime | None = None) -> tuple[str, ...]:
        current = _stamp(now or datetime.now(UTC))
        recovered: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE state IN
                    ('SCOPING','PLANNING','READY','EXECUTING','VERIFYING','WAITING')
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (current,),
            ).fetchall()
            for row in rows:
                recovered.append(row["run_id"])
                connection.execute(
                    """
                    UPDATE runs
                    SET state = 'INTERRUPTED', updated_at = ?,
                        lease_owner = NULL, lease_expires_at = NULL,
                        failure = 'worker lease expired',
                        failure_class = 'worker_lease_expired',
                        recoverable = 1
                    WHERE run_id = ?
                    """,
                    (current, row["run_id"]),
                )
                self._fail_open_intents(
                    connection,
                    row["run_id"],
                    current,
                    "worker lease expired before reconciliation",
                )
            if recovered:
                task = self._load_task(connection)
                self._set_meta(connection, "activity_status", "paused")
                self._set_meta(connection, "current_run_id", None)
                if task.state not in {TaskState.COMPLETED, TaskState.SUPERSEDED}:
                    self._set_meta(
                        connection,
                        "task",
                        replace(task, state=TaskState.PAUSED).to_dict(),
                    )
                for run_id in recovered:
                    self._insert_event(
                        connection,
                        task.task_id,
                        "run.interrupted",
                        {"run_id": run_id, "reason": "lease expired"},
                    )
                self._touch(connection)
        if recovered:
            # An interrupted run is a closed run: its scratch must go too,
            # which is the case the old code never reached.
            for run_id in recovered:
                self._discard_scratch(run_id)
            self._release_browser_resources()
            self._render_readme()
        return tuple(recovered)

    def runs(self, task_id: str | None = None) -> tuple[TaskRun, ...]:
        if task_id is not None and self.active_task_id is None:
            return self.bind(task_id).runs()
        with self._read(self._database(task_id)) as connection:
            return self._load_runs(connection)

    def resume(
        self,
        *,
        lease_owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> TaskRun:
        # The common crash shape is one workspace with one still-active run;
        # recovering first makes resume work instead of reporting no terminal
        # run to resume.
        self.recover_expired_runs()
        with self._read(self._database()) as connection:
            row = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE state IN ('INTERRUPTED', 'FAILED', 'CANCELLED', 'SUCCEEDED')
                ORDER BY updated_at DESC, run_id DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            raise ValueError("workspace has no terminal run to resume")
        return self.start_run(
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            resumes_run_id=row["run_id"],
        )

    def store_run_artifact(
        self,
        run_id: str,
        category: str,
        source: Path,
        name: str,
        *,
        lease_owner: str | None = None,
    ) -> dict:
        with self._filesystem_lock():
            return self._store_run_artifact(
                run_id, category, source, name, lease_owner=lease_owner
            )

    def _store_run_artifact(
        self,
        run_id: str,
        category: str,
        source: Path,
        name: str,
        *,
        lease_owner: str | None = None,
    ) -> dict:
        if category not in {"evidence", "receipts", "delegations", "scratch"}:
            raise ValueError("unsupported run artifact category")
        # Resolve an earlier interrupted store first: writing a new journal
        # entry over an unresolved one would strand the previous file with no
        # record of it anywhere.
        self._resolve_pending_artifact()
        if not DELIVERABLE_NAME.fullmatch(name) or name in {".", ".."}:
            raise ValueError("artifact name must be one safe path component")
        if source.is_symlink():
            raise ValueError("artifact source must not be a symlink")
        source = source.resolve()
        if not source.is_file():
            raise ValueError("artifact source must be a regular file")
        with self._read(self._database()) as connection:
            run = connection.execute(
                "SELECT state, lease_owner FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not run:
                raise ValueError("artifact run does not exist")
            active = RunState(run["state"]) in ACTIVE_RUN_STATES
            if category == "scratch" and not active:
                raise ValueError("scratch artifacts require an active run")
            if active and run["lease_owner"] != lease_owner:
                # Writing into a live run is a write by that run; a caller
                # without its lease must not do it. Evidence for an already
                # terminal run stays allowed so an outcome can be captured
                # after the fact.
                raise PermissionError(
                    "storing an artifact for an active run requires its lease owner"
                )
        relative = Path(".task") / "runs" / run_id / category / name
        destination = self.path() / relative
        category_root = destination.parent
        # `exists()` is false for a dangling symlink, so test the link itself.
        if any(
            part.is_symlink()
            for part in (
                self.path() / ".task",
                self.path() / ".task" / "runs",
                self.path() / ".task" / "runs" / run_id,
                category_root,
                destination,
            )
        ):
            raise ValueError("run artifact storage is unsafe")
        category_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"run artifact already exists: {name}")
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{name}.",
            dir=category_root,
        )
        os.close(fd)
        temporary = Path(temporary_name)
        artifact_id = uuid7()
        placed = False
        try:
            shutil.copy2(source, temporary)
            os.chmod(temporary, 0o600)
            digest = self._digest_path(temporary)
            pending = {
                "artifact_id": artifact_id,
                "run_id": run_id,
                "category": category,
                "path": relative.as_posix(),
                "sha256": digest,
                "started_at": _now(),
            }
            # Recorded before the visible rename so a crash in the window is
            # recognisable as an interrupted store rather than damage.
            self._write_journal("pending_artifact", pending)
            os.replace(temporary, destination)
            placed = True
            self._fsync_directory(category_root)
            with self._transaction() as connection:
                state = connection.execute(
                    "SELECT state FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if not state:
                    raise ValueError("artifact run does not exist")
                if (
                    category == "scratch"
                    and RunState(state["state"]) not in ACTIVE_RUN_STATES
                ):
                    raise ValueError("scratch artifacts require an active run")
                self._insert_artifact_row(connection, pending)
                self._set_meta(connection, "pending_artifact", None)
                self._touch(connection)
        except Exception:
            if placed and destination.exists():
                destination.unlink()
            # Clear only our own entry: an unrelated pending record must not be
            # discarded by this failure.
            self._clear_own_artifact_journal(artifact_id)
            raise
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "category": category,
            "path": relative.as_posix(),
            "sha256": digest,
        }

    def _write_journal(self, key: str, value: dict | None) -> None:
        with self._transaction() as connection:
            self._set_meta(connection, key, value)

    def _clear_own_artifact_journal(self, artifact_id: str) -> None:
        with self._transaction() as connection:
            pending = self._all_meta(connection).get("pending_artifact")
            if pending and pending.get("artifact_id") == artifact_id:
                self._set_meta(connection, "pending_artifact", None)

    def _insert_artifact_row(
        self, connection: sqlite3.Connection, pending: dict
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO run_artifacts(
                    artifact_id, run_id, category, path, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pending["artifact_id"],
                    pending["run_id"],
                    pending["category"],
                    pending["path"],
                    pending["sha256"],
                    pending["started_at"],
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                f"run artifact is already registered: {pending['path']}"
            ) from error
        self._insert_event(
            connection,
            self._id(),
            "run.artifact_stored",
            {
                "artifact_id": pending["artifact_id"],
                "run_id": pending["run_id"],
                "category": pending["category"],
                "path": pending["path"],
                "sha256": pending["sha256"],
            },
        )

    def _discard_scratch(self, run_id: str) -> None:
        """Delete a run's scratch, rows first, and stay safe to repeat.

        Rows are authoritative: an orphan file is reported as unregistered and
        cleaned by the next call, while an orphan row would be reported as
        damage forever.
        """

        with self._filesystem_lock():
            with self._transaction() as connection:
                connection.execute(
                    "DELETE FROM run_artifacts "
                    "WHERE run_id = ? AND category = 'scratch'",
                    (run_id,),
                )
            scratch = self.path() / ".task" / "runs" / run_id / "scratch"
            if scratch.is_symlink():
                scratch.unlink()
            elif scratch.is_dir():
                shutil.rmtree(scratch, ignore_errors=True)
            for parent in (scratch.parent, scratch.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    pass

    def publish_deliverable(
        self,
        source: Path,
        name: str,
        *,
        kind: str,
        description: str,
        reusable: bool = False,
        verified: bool = False,
        entrypoint: str | None = None,
        produced_by_run: str | None = None,
    ) -> Deliverable:
        with self._filesystem_lock():
            return self._publish_deliverable(
                source,
                name,
                kind=kind,
                description=description,
                reusable=reusable,
                verified=verified,
                entrypoint=entrypoint,
                produced_by_run=produced_by_run,
            )

    def _publish_deliverable(
        self,
        source: Path,
        name: str,
        *,
        kind: str,
        description: str,
        reusable: bool = False,
        verified: bool = False,
        entrypoint: str | None = None,
        produced_by_run: str | None = None,
    ) -> Deliverable:
        if not DELIVERABLE_NAME.fullmatch(name) or name in {".", ".."}:
            raise ValueError("deliverable name must be one safe path component")
        kind = collapse_free_text(kind, field="deliverable kind")
        description = collapse_free_text(
            description, field="deliverable description"
        )
        if reusable and kind in {"browser-automation", "script"} and not entrypoint:
            raise ValueError("reusable executable deliverable requires an entrypoint")
        if source.is_symlink():
            raise ValueError("deliverable source must not be a symlink")
        source = source.resolve()
        self._validate_source_tree(source)
        workspace = self.path()
        private = workspace / ".task"
        # Finish or roll back an interrupted publish before judging what the
        # visible tree means, otherwise a crashed write looks like a user edit.
        _, unresolved = self._resolve_pending_publish()
        if unresolved:
            raise ValueError("; ".join(unresolved))
        staging = private / f".publish-{uuid7()}"
        target_root = workspace / "deliverables"
        target = target_root / name
        previous: Deliverable | None = None
        with self._read(self._database()) as connection:
            if produced_by_run:
                run = connection.execute(
                    "SELECT state FROM runs WHERE run_id = ?",
                    (produced_by_run,),
                ).fetchone()
                if not run:
                    raise ValueError("producing run does not exist")
            row = connection.execute(
                "SELECT * FROM deliverables WHERE name = ?", (name,)
            ).fetchone()
            if row:
                previous = self._deliverable_from_row(row)
        if target.exists():
            if not previous:
                raise ValueError("visible deliverable is not registered")
            self._validate_source_tree(target)
            if self._digest_path(target) != previous.sha256:
                raise ValueError("published deliverable was modified by the user")

        swapped = False
        try:
            if source.is_dir():
                # symlinks=True keeps links as links so the staged tree can be
                # rejected below; the default follows them and would inline the
                # target's bytes, making the post-copy check unable to see it.
                shutil.copytree(source, staging, symlinks=True)
            else:
                shutil.copy2(source, staging, follow_symlinks=False)
            self._validate_source_tree(staging)
            self._validate_entrypoint(staging, name, entrypoint)
            digest = self._digest_path(staging)
            revision = (previous.revision + 1) if previous else 1
            if target_root.is_symlink():
                raise ValueError("deliverables directory must not be a symlink")
            target_root.mkdir(mode=0o700, exist_ok=True)
            if target_root.resolve().parent != workspace.resolve():
                raise ValueError("deliverables directory escapes workspace")
            backup: Path | None = None
            if target.exists():
                version_root = private / "versions" / name
                versions = private / "versions"
                if versions.is_symlink() or version_root.is_symlink():
                    raise ValueError("deliverable version storage is unsafe")
                version_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                backup = self._unused_backup_path(version_root, previous.revision)
            deliverable = Deliverable(
                name=name,
                task_id=self._id(),
                path=f"deliverables/{name}",
                kind=kind,
                sha256=digest,
                revision=revision,
                produced_by_run=produced_by_run,
                reusable=reusable,
                verified=verified,
                description=description,
                entrypoint=entrypoint,
            )
            pending = {
                "deliverable": asdict(deliverable),
                "staging": staging.name,
                "backup": backup.name if backup else None,
                "previous_sha256": previous.sha256 if previous else None,
                "previous_revision": previous.revision if previous else None,
                "started_at": _now(),
            }
            # Committed before any visible rename: this record is what turns a
            # crashed publish into a repairable state instead of a permanent
            # "modified by the user" accusation.
            self._write_journal("pending_publish", pending)
            if backup:
                os.replace(target, backup)
            try:
                os.replace(staging, target)
                swapped = True
            except Exception:
                if backup and backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
            self._fsync_directory(target_root)
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO deliverables(
                        name, path, kind, sha256, revision, produced_by_run,
                        reusable, verified, description, entrypoint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        path = excluded.path,
                        kind = excluded.kind,
                        sha256 = excluded.sha256,
                        revision = excluded.revision,
                        produced_by_run = excluded.produced_by_run,
                        reusable = excluded.reusable,
                        verified = excluded.verified,
                        description = excluded.description,
                        entrypoint = excluded.entrypoint
                    """,
                    (
                        deliverable.name,
                        deliverable.path,
                        deliverable.kind,
                        deliverable.sha256,
                        deliverable.revision,
                        deliverable.produced_by_run,
                        int(deliverable.reusable),
                        int(deliverable.verified),
                        deliverable.description,
                        deliverable.entrypoint,
                    ),
                )
                self._set_meta(connection, "pending_publish", None)
                self._touch(connection)
                self._insert_event(
                    connection,
                    deliverable.task_id,
                    "deliverable.published",
                    {
                        "name": name,
                        "sha256": digest,
                        "revision": revision,
                        "reusable": reusable,
                    },
                )
        except Exception:
            # Only drop the journal entry when the visible tree was never
            # touched; otherwise it is what lets repair finish the publish.
            if not swapped:
                self._write_journal("pending_publish", None)
            raise
        finally:
            if staging.is_symlink():
                staging.unlink()
            elif staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
            elif staging.exists():
                staging.unlink()
        self._render_readme()
        return deliverable

    @staticmethod
    def _unused_backup_path(version_root: Path, revision: int) -> Path:
        """Never overwrite an existing revision backup on a republish retry."""

        candidate = version_root / f"r{revision}"
        if not candidate.exists():
            return candidate
        return version_root / f"r{revision}.{uuid7()}"

    @staticmethod
    def _validate_entrypoint(
        staging: Path, name: str, entrypoint: str | None
    ) -> Path | None:
        if not entrypoint:
            return None
        candidate = Path(entrypoint)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or any(part in {"..", "."} for part in candidate.parts)
        ):
            raise ValueError(
                "deliverable entrypoint must be a relative in-tree path"
            )
        if any(not DELIVERABLE_NAME.fullmatch(part) for part in candidate.parts):
            raise ValueError(
                "deliverable entrypoint must use safe path components"
            )
        if staging.is_dir():
            entry = staging / candidate
            if not entry.resolve().is_relative_to(staging.resolve()):
                raise ValueError("deliverable entrypoint escapes the deliverable")
        elif candidate.as_posix() == name:
            # A one-file reusable script names itself; requiring a directory
            # made that combination impossible to publish at all.
            entry = staging
        else:
            raise ValueError(
                "a single-file deliverable must use its own name as the entrypoint"
            )
        if entry.is_symlink() or not entry.is_file():
            raise ValueError("deliverable entrypoint is missing or unsafe")
        if not os.access(entry, os.X_OK):
            raise ValueError("deliverable entrypoint is not executable")
        return entry

    def repair(
        self, *, adopt_visible: bool = False, discard_journal: bool = False
    ) -> dict:
        """Resolve interrupted filesystem operations and stale debris.

        Every state `doctor` can report as damage has to have a way out that is
        not manual surgery inside `.task/`. Actions and diagnostics are reported
        separately: a state this call could not resolve must not be logged as a
        repair, and the caller has to be able to tell the difference.
        """

        actions: list[str] = []
        diagnostics: list[str] = []
        with self._filesystem_lock():
            performed, unresolved = self._resolve_pending_publish(
                adopt_visible=adopt_visible, discard_journal=discard_journal
            )
            actions.extend(performed)
            diagnostics.extend(unresolved)
            actions.extend(self._resolve_pending_artifact())
            actions.extend(self._discard_orphan_staging())
            actions.extend(self._discard_terminal_scratch())
            actions.extend(self._resolve_orphan_artifacts())
        if self._render_readme_if_stale():
            actions.append("regenerated README.md")
        if actions:
            with self._transaction() as connection:
                self._insert_event(
                    connection,
                    self._id(),
                    "workspace.repaired",
                    {"actions": list(actions)},
                )
                self._touch(connection)
        return {"actions": tuple(actions), "diagnostics": tuple(diagnostics)}

    def _resolve_pending_publish(
        self, *, adopt_visible: bool = False, discard_journal: bool = False
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._read(self._database()) as connection:
            pending = self._all_meta(connection).get("pending_publish")
        if not pending:
            return ((), ())
        record = Deliverable(**pending["deliverable"])
        workspace = self.path()
        target = workspace / record.path
        backup_name = pending.get("backup")
        backup = (
            workspace / ".task" / "versions" / record.name / backup_name
            if backup_name
            else None
        )
        if target.exists() and self._digest_path(target) == record.sha256:
            self._register_pending_publish(record)
            return ((f"completed interrupted publish: {record.name}",), ())
        if backup and backup.exists() and not target.exists():
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(backup, target)
            self._write_journal("pending_publish", None)
            return (
                (
                    "restored deliverable after interrupted publish: "
                    f"{record.name}",
                ),
                (),
            )
        previous_sha = pending.get("previous_sha256")
        if target.exists() and previous_sha and self._digest_path(target) == previous_sha:
            self._write_journal("pending_publish", None)
            return ((f"rolled back interrupted publish: {record.name}",), ())
        if not target.exists() and not previous_sha:
            self._write_journal("pending_publish", None)
            return ((f"discarded interrupted first publish: {record.name}",), ())
        if adopt_visible and target.exists():
            adopted = replace(record, sha256=self._digest_path(target))
            self._register_pending_publish(adopted)
            return (
                (f"adopted visible content as {record.name} r{adopted.revision}",),
                (),
            )
        if discard_journal:
            self._write_journal("pending_publish", None)
            return ((f"discarded publish journal for {record.name}",), ())
        return (
            (),
            (
                f"unresolved publish journal for {record.name}: visible content "
                "matches neither the staged nor the previous digest; rerun with "
                "--adopt-visible to register what is on disk or "
                "--discard-journal to drop the record",
            ),
        )

    def _resolve_orphan_artifacts(self) -> tuple[str, ...]:
        """Adopt or drop run artifacts on disk that no row covers.

        Without this an interrupted store that was later overwritten in the
        journal left a file `doctor` reported forever and `repair` ignored.
        """

        workspace = self.path()
        runs_root = workspace / ".task" / "runs"
        if not runs_root.is_dir() or runs_root.is_symlink():
            return ()
        with self._read(self._database()) as connection:
            registered = {
                row["path"]
                for row in connection.execute("SELECT path FROM run_artifacts")
            }
            known_runs = {
                row["run_id"]
                for row in connection.execute("SELECT run_id FROM runs")
            }
        resolved: list[str] = []
        for candidate in sorted(runs_root.rglob("*")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(workspace).as_posix()
            if relative in registered:
                continue
            parts = candidate.relative_to(runs_root).parts
            if len(parts) != 3:
                continue
            run_id, category, name = parts
            if run_id not in known_runs or category == "scratch":
                candidate.unlink()
                resolved.append(f"discarded orphan run artifact: {relative}")
                continue
            pending = {
                "artifact_id": uuid7(),
                "run_id": run_id,
                "category": category,
                "path": relative,
                "sha256": self._digest_path(candidate),
                "started_at": _now(),
            }
            with self._transaction() as connection:
                self._insert_artifact_row(connection, pending)
                self._touch(connection)
            resolved.append(f"adopted orphan run artifact: {relative}")
        return tuple(resolved)

    def _register_pending_publish(self, record: Deliverable) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO deliverables(
                    name, path, kind, sha256, revision, produced_by_run,
                    reusable, verified, description, entrypoint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    path = excluded.path,
                    kind = excluded.kind,
                    sha256 = excluded.sha256,
                    revision = excluded.revision,
                    produced_by_run = excluded.produced_by_run,
                    reusable = excluded.reusable,
                    verified = excluded.verified,
                    description = excluded.description,
                    entrypoint = excluded.entrypoint
                """,
                (
                    record.name,
                    record.path,
                    record.kind,
                    record.sha256,
                    record.revision,
                    record.produced_by_run,
                    int(record.reusable),
                    int(record.verified),
                    record.description,
                    record.entrypoint,
                ),
            )
            self._set_meta(connection, "pending_publish", None)
            self._insert_event(
                connection,
                self._id(),
                "deliverable.publish_recovered",
                {
                    "name": record.name,
                    "sha256": record.sha256,
                    "revision": record.revision,
                },
            )
            self._touch(connection)

    def _resolve_pending_artifact(self) -> tuple[str, ...]:
        with self._read(self._database()) as connection:
            pending = self._all_meta(connection).get("pending_artifact")
            registered = bool(
                connection.execute(
                    "SELECT 1 FROM run_artifacts WHERE artifact_id = ?",
                    ((pending or {}).get("artifact_id"),),
                ).fetchone()
            )
        if not pending:
            return ()
        target = self.path() / pending["path"]
        if registered:
            self._write_journal("pending_artifact", None)
            return ()
        if (
            target.is_file()
            and not target.is_symlink()
            and self._digest_path(target) == pending["sha256"]
        ):
            with self._transaction() as connection:
                self._insert_artifact_row(connection, pending)
                self._set_meta(connection, "pending_artifact", None)
                self._touch(connection)
            return (f"registered interrupted run artifact: {pending['path']}",)
        if target.exists() or target.is_symlink():
            target.unlink()
        self._write_journal("pending_artifact", None)
        return (f"discarded interrupted run artifact: {pending['path']}",)

    def _discard_orphan_staging(self) -> tuple[str, ...]:
        private = self.path() / ".task"
        if not private.is_dir() or private.is_symlink():
            return ()
        with self._read(self._database()) as connection:
            pending = self._all_meta(connection).get("pending_publish")
        keep = (pending or {}).get("staging")
        removed: list[str] = []
        for staging in private.glob(".publish-*"):
            if staging.name == keep:
                continue
            if staging.is_symlink():
                staging.unlink()
            elif staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
            else:
                staging.unlink()
            removed.append(f"removed abandoned publish staging: {staging.name}")
        return tuple(removed)

    def _discard_terminal_scratch(self) -> tuple[str, ...]:
        with self._read(self._database()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT run_artifacts.run_id AS run_id
                FROM run_artifacts JOIN runs USING (run_id)
                WHERE run_artifacts.category = 'scratch'
                  AND runs.state IN
                      ('SUCCEEDED','INTERRUPTED','FAILED','CANCELLED')
                """
            ).fetchall()
        cleaned: list[str] = []
        for row in rows:
            self._discard_scratch(row["run_id"])
            cleaned.append(f"pruned scratch of terminal run: {row['run_id']}")
        return tuple(cleaned)

    def deliverables(
        self, task_id: str | None = None
    ) -> tuple[Deliverable, ...]:
        if task_id is not None and self.active_task_id is None:
            return self.bind(task_id).deliverables()
        with self._read(self._database(task_id)) as connection:
            return self._load_deliverables(connection)

    def add_alias(self, alias: str) -> None:
        normalized = normalize_search(alias)
        if not normalized:
            raise ValueError("alias must contain searchable text")
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO aliases(alias) VALUES (?)", (normalized,)
            )
            self._touch(connection)

    def doctor(self, task_id: str | None = None) -> tuple[str, ...]:
        if task_id is not None and self.active_task_id is None:
            return self.bind(task_id).doctor()
        issues: list[str] = []
        workspace = self.path(task_id)
        if workspace.is_symlink() or not workspace.is_dir():
            return ("workspace root is invalid",)
        database = self._database(task_id)
        try:
            with self._read(database) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    issues.append(f"sqlite integrity: {integrity}")
                issues.extend(
                    self._consistency_issues(
                        connection, workspace, include_readme=True
                    )
                )
        except (OSError, sqlite3.Error, ValueError) as error:
            issues.append(f"metadata damaged: {error}")
        return tuple(issues)

    def _consistency_issues(
        self,
        connection: sqlite3.Connection,
        workspace: Path,
        *,
        include_readme: bool,
    ) -> tuple[str, ...]:
        """One definition of workspace health.

        `doctor` and `complete` used to disagree, so a task could complete in a
        state the health check reported as broken.
        """

        issues: list[str] = []
        metadata = self._all_meta(connection)
        registered = self._load_deliverables(connection)
        for deliverable in registered:
            target = workspace / deliverable.path
            if not target.exists():
                issues.append(f"missing deliverable: {deliverable.name}")
                continue
            try:
                self._validate_source_tree(target)
            except ValueError:
                issues.append(f"unsafe deliverable: {deliverable.name}")
                continue
            if self._digest_path(target) != deliverable.sha256:
                issues.append(f"modified deliverable: {deliverable.name}")
        for row in connection.execute(
            """
            SELECT action_id, status FROM actions
            WHERE status IN ('INTENDED', 'OUTCOME_UNKNOWN')
            ORDER BY action_id
            """
        ):
            issues.append(
                f"unresolved action: {row['action_id']} ({row['status']})"
            )
        artifact_rows = connection.execute(
            """
            SELECT artifact_id, path, sha256 FROM run_artifacts
            ORDER BY artifact_id
            """
        ).fetchall()
        registered_artifact_paths = {row["path"] for row in artifact_rows}
        for row in artifact_rows:
            target = workspace / row["path"]
            if (
                target.is_symlink()
                or not target.is_file()
                or self._digest_path(target) != row["sha256"]
            ):
                issues.append(f"damaged run artifact: {row['artifact_id']}")
        deliverable_root = workspace / "deliverables"
        if deliverable_root.is_symlink():
            issues.append("deliverables directory is unsafe")
        elif deliverable_root.is_dir():
            registered_names = {item.name for item in registered}
            for child in deliverable_root.iterdir():
                if child.name not in registered_names:
                    issues.append(f"unregistered deliverable: {child.name}")
        private = workspace / ".task"
        if private.is_dir() and not private.is_symlink():
            for staging in private.glob(".publish-*"):
                issues.append(f"incomplete publish staging: {staging.name}")
            runs_root = private / "runs"
            if runs_root.is_symlink():
                issues.append("run artifact directory is unsafe")
            elif runs_root.is_dir():
                for artifact in runs_root.rglob("*"):
                    if artifact.is_symlink():
                        issues.append(
                            "unsafe run artifact path: "
                            f"{artifact.relative_to(workspace)}"
                        )
                    elif artifact.is_file():
                        relative = artifact.relative_to(workspace).as_posix()
                        if relative not in registered_artifact_paths:
                            issues.append(
                                f"unregistered run artifact: {relative}"
                            )
        for row in connection.execute(
            """
            SELECT DISTINCT run_artifacts.run_id AS run_id
            FROM run_artifacts JOIN runs USING (run_id)
            WHERE run_artifacts.category = 'scratch'
              AND runs.state IN ('SUCCEEDED','INTERRUPTED','FAILED','CANCELLED')
            ORDER BY run_id
            """
        ):
            issues.append(
                f"scratch survives terminal run: {row['run_id']}; run task repair"
            )
        for key, label in (
            ("pending_publish", "publish"),
            ("pending_artifact", "run artifact"),
        ):
            if metadata.get(key):
                issues.append(
                    f"interrupted {label} recorded in the journal; run task repair"
                )
        now = _now()
        for row in connection.execute(
            """
            SELECT run_id, lease_expires_at FROM runs
            WHERE state IN
                ('SCOPING','PLANNING','READY','EXECUTING','VERIFYING','WAITING')
              AND lease_expires_at IS NOT NULL
            ORDER BY run_id
            """
        ):
            if row["lease_expires_at"] < now:
                # Otherwise `status` keeps reporting "running" for a worker
                # that died, with nothing pointing at `task recover`.
                issues.append(
                    f"expired lease on active run: {row['run_id']}; run task recover"
                )
        for problem in self._event_chain_issues(connection):
            issues.append(problem)
        if include_readme:
            readme = workspace / "README.md"
            if not readme.is_file() or readme.is_symlink():
                issues.append("README.md is missing or unsafe")
            elif readme.read_text(encoding="utf-8") != self._expected_readme(
                connection
            ):
                issues.append("README.md does not reflect the recorded state")
        return tuple(issues)

    @staticmethod
    def _event_chain_issues(connection: sqlite3.Connection) -> tuple[str, ...]:
        """Detect a rewritten or deleted audit row."""

        previous = ""
        for row in connection.execute(
            "SELECT sequence, payload_json FROM events ORDER BY sequence"
        ):
            payload = json.loads(row["payload_json"])
            # Rows written before chaining existed carry no link; the chain
            # starts where chaining started, and every linked row is verified.
            if "prev_event_sha256" in payload:
                if payload["prev_event_sha256"] != previous:
                    return (f"audit chain broken at event {row['sequence']}",)
            previous = hashlib.sha256(
                (previous + row["payload_json"]).encode("utf-8")
            ).hexdigest()
        return ()

    def list_tasks(self, *, include_archived: bool = False) -> tuple[Task, ...]:
        if not self.tasks.exists():
            return ()
        found: list[Task] = []
        for path in self._workspace_candidates():
            try:
                candidate = TaskStore(self.root, path.name)
                if (
                    not include_archived
                    and candidate._meta().get("archived_at")
                ):
                    continue
                found.append(candidate.load())
            except (OSError, ValueError, sqlite3.Error):
                continue
        return tuple(found)

    def _workspace_candidates(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in sorted(self.tasks.iterdir())
            if path.is_dir()
            and not path.is_symlink()
            and WORKSPACE_SLUG.fullmatch(path.name)
            and (path / ".task" / "state.sqlite").is_file()
        )

    def damaged_workspaces(self) -> tuple[dict, ...]:
        """Workspaces that exist on disk but cannot be listed.

        Silently skipping them hid corrupt state and invited a duplicate
        workspace for an intent that already had one.
        """

        if not self.tasks.exists():
            return ()
        damaged: list[dict] = []
        for path in self._workspace_candidates():
            if TIMESTAMP_TASK_ID.fullmatch(path.name) or path.name in GENERIC_SLUGS:
                damaged.append(
                    {"task_id": path.name, "reason": "opaque legacy workspace name"}
                )
                continue
            try:
                TaskStore(self.root, path.name).load()
            except (OSError, ValueError, sqlite3.Error) as error:
                damaged.append({"task_id": path.name, "reason": str(error)})
        return tuple(damaged)

    def find(self, query: str) -> tuple[Task, ...]:
        terms = {item for item in normalize_search(query).split() if item}
        if not terms:
            return ()
        scored: list[tuple[int, str, Task]] = []
        for task in self.list_tasks(include_archived=True):
            # A bound store must still be able to search: reading another
            # workspace through `self` trips the binding guard.
            peer = TaskStore(self.root, task.task_id)
            with peer._read(peer._database()) as connection:
                metadata = self._all_meta(connection)
                aliases = " ".join(
                    row["alias"]
                    for row in connection.execute("SELECT alias FROM aliases")
                )
                deliverables = " ".join(
                    f"{row['name']} {row['kind']} {row['description']}"
                    for row in connection.execute(
                        "SELECT name, kind, description FROM deliverables"
                    )
                )
            haystack = normalize_search(
                " ".join(
                    (
                        task.task_id,
                        str(metadata.get("title") or ""),
                        task.goal,
                        aliases,
                        deliverables,
                    )
                )
            )
            words = set(haystack.split())
            # A four-character prefix made "track" match "trackpad"; reuse
            # decisions hinge on this, so require a full token or a six
            # character prefix.
            score = sum(
                1
                for term in terms
                if term in words
                or (
                    len(term) >= 6
                    and any(
                        word.startswith(term[:6]) or term.startswith(word[:6])
                        for word in words
                    )
                )
            )
            reusable = any(
                item.reusable and item.verified for item in peer.deliverables()
            )
            if score:
                archived_penalty = 2 if metadata.get("archived_at") else 0
                superseded_penalty = 4 if task.state is TaskState.SUPERSEDED else 0
                scored.append(
                    (
                        score * 10
                        + int(reusable)
                        - archived_penalty
                        - superseded_penalty,
                        task.task_id,
                        task,
                    )
                )
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in scored)

    def _database(self, task_id: str | None = None) -> Path:
        private = self.path(task_id) / ".task"
        if private.is_symlink() or not private.is_dir():
            raise ValueError("invalid workspace metadata directory")
        target = private / "state.sqlite"
        if target.is_symlink() or not target.is_file():
            raise ValueError("invalid workspace metadata")
        if target.resolve().parent != private.resolve():
            raise ValueError("workspace metadata escapes task directory")
        return target

    def _meta(self, task_id: str | None = None) -> dict:
        with self._read(self._database(task_id)) as connection:
            return self._all_meta(connection)

    @staticmethod
    def _connect(database: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _read(self, database: Path):
        """Read-only connection that is always closed.

        `sqlite3.Connection.__enter__` manages the transaction, not the handle,
        so a plain `with self._connect(...)` leaks the handle and its read lock
        until the garbage collector runs.
        """

        connection = self._connect(database)
        try:
            yield connection
        finally:
            connection.close()

    def _transaction(self):
        class Transaction:
            def __init__(self, store: TaskStore):
                self.store = store

            def __enter__(self):
                self.connection = self.store._connect(self.store._database())
                self.connection.execute("BEGIN IMMEDIATE")
                return self.connection

            def __exit__(self, error_type, *_):
                try:
                    if error_type is None:
                        self.connection.commit()
                    else:
                        self.connection.rollback()
                finally:
                    # A failing commit (disk full, busy at commit time) must
                    # still release the handle and its locks.
                    self.connection.close()

        return Transaction(self)

    @contextmanager
    def _filesystem_lock(self):
        """Serialize visible-file mutations for one workspace.

        Re-entrant within a store instance so a locked operation can call
        another one (publish -> pending resolution -> scratch discard) without
        deadlocking on a second file description.
        """

        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        private = self.path() / ".task"
        if private.is_symlink() or not private.is_dir():
            raise ValueError("workspace metadata directory is unsafe")
        fd = os.open(private, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._lock_depth = 1
            yield
        finally:
            self._lock_depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist a rename so a crash cannot lose the renamed entry."""

        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            PRAGMA user_version = 1;
            CREATE TABLE metadata(
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE aliases(alias TEXT PRIMARY KEY);
            CREATE TABLE authorizations(
                grant_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL
            );
            CREATE TABLE runs(
                run_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN (
                    'SCOPING','PLANNING','READY','EXECUTING','VERIFYING',
                    'WAITING','SUCCEEDED','INTERRUPTED','FAILED','CANCELLED'
                )),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                resumes_run_id TEXT REFERENCES runs(run_id),
                checkpoint TEXT,
                failure TEXT,
                failure_class TEXT,
                recoverable INTEGER CHECK(recoverable IN (0, 1))
            );
            CREATE TABLE deliverables(
                name TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                produced_by_run TEXT REFERENCES runs(run_id),
                reusable INTEGER NOT NULL CHECK(reusable IN (0, 1)),
                verified INTEGER NOT NULL CHECK(verified IN (0, 1)),
                description TEXT NOT NULL,
                entrypoint TEXT
            );
            CREATE TABLE actions(
                action_id TEXT PRIMARY KEY,
                run_id TEXT REFERENCES runs(run_id),
                action_class TEXT NOT NULL CHECK(action_class IN (
                    'observe','navigate','prepare_mutation','commit_external',
                    'credential_or_identity','financial','destructive'
                )),
                target TEXT NOT NULL,
                summary_sha256 TEXT NOT NULL,
                content_sha256 TEXT,
                status TEXT NOT NULL CHECK(status IN (
                    'INTENDED','VERIFIED','FAILED','OUTCOME_UNKNOWN'
                )),
                outcome TEXT,
                evidence_sha256 TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE run_artifacts(
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                category TEXT NOT NULL CHECK(category IN (
                    'evidence','receipts','delegations','scratch'
                )),
                path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX runs_state_index ON runs(state, lease_expires_at);
            CREATE INDEX actions_status_index ON actions(status, action_class);
            CREATE INDEX artifacts_run_index
                ON run_artifacts(run_id, category);
            """
        )

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value) -> None:
        connection.execute(
            """
            INSERT INTO metadata(key, value_json) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """,
            (key, json.dumps(value, sort_keys=True)),
        )

    @staticmethod
    def _all_meta(connection: sqlite3.Connection) -> dict:
        return {
            row["key"]: json.loads(row["value_json"])
            for row in connection.execute("SELECT key, value_json FROM metadata")
        }

    @staticmethod
    def _load_task(connection: sqlite3.Connection) -> Task:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != STORAGE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported task schema: {version}; "
                f"expected {STORAGE_SCHEMA_VERSION}"
            )
        row = connection.execute(
            "SELECT value_json FROM metadata WHERE key = 'task'"
        ).fetchone()
        if not row:
            raise ValueError("missing task metadata")
        data = json.loads(row["value_json"])
        return Task(
            task_id=data["task_id"],
            goal=data["goal"],
            created_at=data["created_at"],
            state=TaskState(data["state"]),
            constraints=tuple(data.get("constraints", [])),
            delegation_policy=data.get("delegation_policy", "maximal"),
            authorization_policy=data.get("authorization_policy", "explicit"),
            browser_policy=data.get("browser_policy", "user_browser_only"),
            allowed_browser_adapters=tuple(
                data.get("allowed_browser_adapters", ["surf"])
            ),
            delegate_provider=data.get("delegate_provider", "chatgpt-web"),
            delegate_transport=data.get("delegate_transport", "surf-ui"),
            reasoning_effort=data.get("reasoning_effort", "best"),
            deep_research_policy=data.get("deep_research_policy", "auto"),
            fallback_policy=data.get("fallback_policy", "block"),
            external_tool_policy=data.get(
                "external_tool_policy", "surf_chatgpt_only"
            ),
            active_browser_adapter=data.get("active_browser_adapter"),
            owned_browser_resources=tuple(
                data.get("owned_browser_resources", [])
            ),
        )

    @staticmethod
    def _touch(connection: sqlite3.Connection) -> None:
        TaskStore._set_meta(connection, "updated_at", _now())

    @staticmethod
    def _event_chain_head(connection: sqlite3.Connection) -> str:
        """Current chain head, cached in metadata.

        Recomputing it from the whole table on every insert made appends
        quadratic in the number of events.
        """

        row = connection.execute(
            "SELECT value_json FROM metadata WHERE key = 'event_chain_head'"
        ).fetchone()
        if row:
            return json.loads(row["value_json"]) or ""
        previous = ""
        for entry in connection.execute(
            "SELECT payload_json FROM events ORDER BY sequence"
        ):
            previous = hashlib.sha256(
                (previous + entry["payload_json"]).encode("utf-8")
            ).hexdigest()
        return previous

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        task_id: str,
        event_type: str,
        payload: dict,
    ) -> None:
        previous = TaskStore._event_chain_head(connection)
        encoded = json.dumps(
            {
                "schema_version": STORAGE_SCHEMA_VERSION,
                "task_id": task_id,
                # Chained so an edited or deleted row is detectable; the log is
                # evidence, not just a convenience.
                "prev_event_sha256": previous,
                **payload,
            },
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO events(event_id, timestamp, event_type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (uuid7(), _now(), event_type, encoded),
        )
        TaskStore._set_meta(
            connection,
            "event_chain_head",
            hashlib.sha256((previous + encoded).encode("utf-8")).hexdigest(),
        )

    def _validate_completion(self, connection: sqlite3.Connection) -> None:
        metadata = self._all_meta(connection)
        summary = str(metadata.get("outcome_summary") or "").strip()
        if not summary:
            raise ValueError("completion requires a substantive outcome summary")
        active = connection.execute(
            "SELECT 1 FROM runs WHERE state IN "
            "('SCOPING','PLANNING','READY','EXECUTING','VERIFYING','WAITING')"
        ).fetchone()
        if active:
            raise ValueError("completion requires every run to be terminal")
        unresolved = connection.execute(
            """
            SELECT action_id FROM actions
            WHERE status IN ('INTENDED', 'OUTCOME_UNKNOWN')
              AND action_class IN (
                  'commit_external',
                  'credential_or_identity',
                  'financial',
                  'destructive'
              )
            LIMIT 1
            """
        ).fetchone()
        if unresolved:
            raise ValueError(
                "completion requires every consequential action to be reconciled"
            )
        for deliverable in self._load_deliverables(connection):
            if not deliverable.verified:
                raise ValueError(
                    f"completion requires verified deliverable: {deliverable.name}"
                )
        # README is rewritten immediately after this transaction commits, so
        # its current content is expected to lag; everything else must already
        # be clean, including the staging and journal state that `complete`
        # previously ignored while `doctor` reported it.
        issues = self._consistency_issues(
            connection, self.path(), include_readme=False
        )
        if issues:
            raise ValueError(
                "completion requires a consistent workspace: " + "; ".join(issues)
            )

    def _expected_readme(self, connection: sqlite3.Connection) -> str:
        task = self._load_task(connection)
        metadata = self._all_meta(connection)
        deliverables = self._load_deliverables(connection)
        return self._readme_text(
            task,
            str(metadata["title"]),
            metadata.get("outcome_summary"),
            deliverables,
            activity=str(metadata.get("activity_status") or "idle"),
            archived_at=metadata.get("archived_at"),
            superseded_by=metadata.get("superseded_by"),
        )

    def _render_readme(self) -> None:
        workspace = self.path()
        with self._read(self._database()) as connection:
            text = self._expected_readme(connection)
        self._atomic_write(workspace / "README.md", text)

    def _render_readme_if_stale(self) -> bool:
        """Re-render the landing page when it lags the recorded state."""

        readme = self.path() / "README.md"
        with self._read(self._database()) as connection:
            expected = self._expected_readme(connection)
        if readme.is_symlink():
            readme.unlink()
        elif readme.is_file() and readme.read_text(encoding="utf-8") == expected:
            return False
        self._atomic_write(readme, expected)
        return True

    @staticmethod
    def _readme_text(
        task: Task,
        title: str,
        outcome: str | None,
        deliverables: Iterable[Deliverable],
        *,
        activity: str = "idle",
        archived_at: str | None = None,
        superseded_by: str | None = None,
    ) -> str:
        status = {
            TaskState.COMPLETED: "Ready to use",
            TaskState.PAUSED: "Paused",
            TaskState.FAILED: "Needs attention",
            TaskState.SUPERSEDED: "Superseded",
        }.get(task.state, task.state.value.title())
        lines = [
            f"# {title}",
            "",
            f"**Status:** {status}",
            f"**Activity:** {activity.title()}",
            "",
            "## Purpose",
            "",
            task.goal,
        ]
        if archived_at:
            lines[4:4] = [f"**Archived:** {archived_at}", ""]
        if superseded_by:
            lines.extend(
                (
                    "",
                    "## Replacement",
                    "",
                    f"This task was replaced by [{superseded_by}](../{superseded_by}/).",
                )
            )
        if outcome:
            lines.extend(("", "## Latest outcome", "", outcome))
        published = tuple(deliverables)
        if published:
            lines.extend(("", "## Deliverables", ""))
            for item in published:
                reuse = " — reusable" if item.reusable else ""
                lines.append(
                    f"- [{item.name}]({item.path}) — {item.description}{reuse}"
                )
        else:
            lines.extend(("", "No deliverables have been published."))
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _atomic_write(target: Path, text: str) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.chmod(temporary, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            TaskStore._fsync_directory(target.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _write_text(target: Path, text: str) -> None:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _grant_dict(grant: AuthorizationGrant) -> dict:
        data = grant.__dict__.copy()
        data["action_class"] = grant.action_class.value
        return data

    @staticmethod
    def _grant_from_dict(data: dict) -> AuthorizationGrant:
        return AuthorizationGrant(
            **{**data, "action_class": ActionClass(data["action_class"])}
        )

    def _load_runs(self, connection: sqlite3.Connection) -> tuple[TaskRun, ...]:
        return tuple(
            self._run_from_row(row)
            for row in connection.execute(
                "SELECT * FROM runs ORDER BY created_at, run_id"
            )
        )

    def _run_from_row(self, row: sqlite3.Row) -> TaskRun:
        return TaskRun(
            run_id=row["run_id"],
            task_id=self._id(),
            state=RunState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            heartbeat_at=row["heartbeat_at"],
            resumes_run_id=row["resumes_run_id"],
            checkpoint=row["checkpoint"],
            failure=row["failure"],
            failure_class=row["failure_class"],
            recoverable=(
                None if row["recoverable"] is None else bool(row["recoverable"])
            ),
        )

    def _load_deliverables(
        self, connection: sqlite3.Connection
    ) -> tuple[Deliverable, ...]:
        return tuple(
            self._deliverable_from_row(row)
            for row in connection.execute(
                "SELECT * FROM deliverables ORDER BY name"
            )
        )

    def _deliverable_from_row(self, row: sqlite3.Row) -> Deliverable:
        return Deliverable(
            name=row["name"],
            task_id=self._id(),
            path=row["path"],
            kind=row["kind"],
            sha256=row["sha256"],
            revision=row["revision"],
            produced_by_run=row["produced_by_run"],
            reusable=bool(row["reusable"]),
            verified=bool(row["verified"]),
            description=row["description"],
            entrypoint=row["entrypoint"],
        )

    @staticmethod
    def _validate_source_tree(source: Path) -> None:
        if source.is_symlink() or (not source.is_file() and not source.is_dir()):
            raise ValueError("deliverable source must be a regular file or directory")
        if source.is_dir():
            for item in source.rglob("*"):
                if item.is_symlink() or (not item.is_file() and not item.is_dir()):
                    raise ValueError(
                        f"deliverable contains an unsafe entry: {item.name}"
                    )

    @staticmethod
    def _digest_path(path: Path) -> str:
        digest = hashlib.sha256()
        if path.is_file():
            digest.update(b"file\0")
            digest.update(f"{path.stat().st_mode & 0o777:o}".encode())
            digest.update(b"\0")
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest()
        digest.update(b"directory\0")
        # The top-level mode is part of the deliverable: without it a chmod on
        # the published directory is an undetectable change.
        digest.update(f"{path.stat().st_mode & 0o777:o}".encode())
        digest.update(b"\0")
        for item in sorted(
            path.rglob("*"),
            key=lambda entry: entry.relative_to(path).as_posix(),
        ):
            relative = item.relative_to(path).as_posix().encode()
            digest.update(b"directory\0" if item.is_dir() else b"file\0")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(f"{item.stat().st_mode & 0o777:o}".encode())
            digest.update(b"\0")
            if item.is_file():
                with item.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                digest.update(b"\0")
        return digest.hexdigest()
