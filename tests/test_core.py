from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from browser_tasks.authorization import summary_sha256
from browser_tasks.delegation import (
    validate_disclosure,
    validate_request_policy,
    validate_response,
)
from browser_tasks.models import (
    ActionClass,
    AuthorizationGrant,
    BrowserAction,
    BrowserObservation,
    DelegationRequest,
    DelegationResponse,
    DisclosureDecision,
    RoutingInput,
    RunState,
    TaskState,
)
from browser_tasks.orchestrator import Orchestrator
from browser_tasks.policy import (
    ensure_external_tool_allowed,
    requires_authorization,
    retry_policy,
)
from browser_tasks.cli import main as cli_main
from browser_tasks.routing import assess
from browser_tasks.scanner import scan_file
from browser_tasks.task_store import TaskStore, normalize_slug
from browser_tasks.verification import verify


def create_task(root: Path, slug: str = "visa-slot-tracker") -> TaskStore:
    TaskStore(root).create(slug, "Build a reusable visa slot tracker")
    return TaskStore(root).bind(slug)


def test_task_store_rejects_traversal_timestamp_generic_and_symlink(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path)
    for invalid in (
        "../escape",
        "20260724-010000-example",
        "task",
        "UPPERCASE",
        "a",
    ):
        with pytest.raises(ValueError):
            store.create(invalid, "Goal")
    (tmp_path / "tasks").mkdir(exist_ok=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "tasks" / "linked-workspace").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        store.path("linked-workspace")


def test_task_store_creates_sparse_human_shape(tmp_path: Path) -> None:
    task = TaskStore(tmp_path).create(
        "visa-slot-tracker",
        "Build a reusable visa slot tracker",
        title="Visa slot tracker",
    )
    root = tmp_path / "tasks" / "visa-slot-tracker"
    assert task.state is TaskState.DRAFT
    assert sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    ) == [".task/state.sqlite", "README.md"]
    assert not (root / "deliverables").exists()
    assert not (root / ".task" / "runs").exists()
    readme = (root / "README.md").read_text()
    assert "# Visa slot tracker" in readme
    assert "Build a reusable visa slot tracker" in readme
    assert "Work in progress" not in readme
    assert root.stat().st_mode & 0o077 == 0
    internal_id = TaskStore(tmp_path).bind("visa-slot-tracker").info()[
        "internal_task_id"
    ]
    assert uuid.UUID(internal_id).version == 7
    events = TaskStore(tmp_path).bind("visa-slot-tracker").audit_events()
    assert events[0]["event_type"] == "task.created"
    assert events[0]["payload"]["task_id"] == "visa-slot-tracker"


def test_task_creation_is_idempotent_only_for_the_same_key(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    first = store.create(
        "visa-slot-tracker",
        "Goal",
        idempotency_key="request-1",
    )
    second = store.create(
        "visa-slot-tracker",
        "Different ignored retry text",
        idempotency_key="request-1",
    )
    assert second == first
    with pytest.raises(FileExistsError):
        store.create("visa-slot-tracker", "Goal")
    with pytest.raises(FileExistsError):
        store.create(
            "visa-slot-tracker",
            "Goal",
            idempotency_key="request-2",
        )


def test_slug_normalization_is_portable() -> None:
    assert normalize_slug("  Visa Slot Tracker — Serbia! ") == (
        "visa-slot-tracker-serbia"
    )


def test_discovery_supports_unicode_aliases(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    store.add_alias("трекер визовых слотов")
    assert TaskStore(tmp_path).find("визовый слот") == (store.load(),)


def test_existing_task_can_enforce_delegate_first_policy(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    store.create(
        "visa-slot-tracker",
        "Goal",
        delegation_policy="off",
    )
    task = store.bind("visa-slot-tracker").enforce_delegate_first_policy()
    assert task.delegation_policy == "maximal"
    assert task.allowed_browser_adapters == ("surf",)
    assert task.delegate_provider == "chatgpt-web"
    assert task.delegate_transport == "surf-ui"
    assert task.reasoning_effort == "best"
    assert task.fallback_policy == "block"


def test_external_tool_guard_is_fail_closed(tmp_path: Path) -> None:
    task = TaskStore(tmp_path).create("visa-slot-tracker", "Goal")
    ensure_external_tool_allowed(task, "browser", "surf")
    ensure_external_tool_allowed(task, "research", "web-chat")
    with pytest.raises(PermissionError, match="firecrawl"):
        ensure_external_tool_allowed(task, "research", "firecrawl")
    with pytest.raises(PermissionError, match="in-app-browser"):
        ensure_external_tool_allowed(task, "browser", "in-app-browser")


def test_run_lease_recovery_prevents_hanging_state(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_seconds=30, lease_owner="worker-1")
    expired = datetime.now(UTC) + timedelta(seconds=31)
    assert store.recover_expired_runs(now=expired) == (run.run_id,)
    recovered = store.runs()[0]
    assert recovered.state is RunState.INTERRUPTED
    assert recovered.failure == "worker lease expired"
    assert recovered.failure_class == "worker_lease_expired"
    assert recovered.recoverable is True
    assert store.load().state is TaskState.PAUSED
    assert store.info()["activity_status"] == "paused"


def test_only_one_active_run_and_resume_links_terminal_run(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    first = store.start_run(lease_owner="worker-1")
    with pytest.raises(ValueError, match="active run"):
        store.start_run()
    store.finish_run(
        first.run_id,
        RunState.INTERRUPTED,
        lease_owner="worker-1",
        checkpoint="after-login",
    )
    resumed = store.resume(
        lease_owner="worker-2",
    )
    assert resumed.resumes_run_id == first.run_id
    store.finish_run(
        resumed.run_id,
        RunState.SUCCEEDED,
        lease_owner="worker-2",
    )


def test_run_lease_owner_is_enforced(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    with pytest.raises(PermissionError, match="another worker"):
        store.heartbeat(run.run_id, lease_owner="worker-2")
    with pytest.raises(PermissionError, match="another worker"):
        store.finish_run(
            run.run_id,
            RunState.CANCELLED,
            lease_owner="worker-2",
        )
    store.finish_run(
        run.run_id,
        RunState.CANCELLED,
        lease_owner="worker-1",
    )


def test_failed_run_requires_structured_recovery_metadata(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    with pytest.raises(ValueError, match="failure class"):
        store.finish_run(
            run.run_id,
            RunState.FAILED,
            lease_owner="worker-1",
            failure="The target rejected the request",
        )
    failed = store.finish_run(
        run.run_id,
        RunState.FAILED,
        lease_owner="worker-1",
        failure="The target rejected the request",
        failure_class="target_rejected",
        recoverable=True,
    )
    assert failed.failure_class == "target_rejected"
    assert failed.recoverable is True


def test_run_artifacts_are_lazy_and_scratch_is_pruned(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    source = tmp_path / "capture.txt"
    source.write_text("captured state")
    evidence = store.store_run_artifact(
        run.run_id,
        "evidence",
        source,
        "before.txt",
        lease_owner="worker-1",
    )
    store.store_run_artifact(
        run.run_id,
        "scratch",
        source,
        "temporary.txt",
        lease_owner="worker-1",
    )
    with pytest.raises(PermissionError, match="lease owner"):
        store.store_run_artifact(
            run.run_id, "evidence", source, "intruder.txt"
        )
    run_root = store.path() / ".task" / "runs" / run.run_id
    assert (run_root / "evidence" / "before.txt").is_file()
    assert (run_root / "scratch" / "temporary.txt").is_file()
    store.finish_run(
        run.run_id,
        RunState.SUCCEEDED,
        lease_owner="worker-1",
    )
    assert not (run_root / "scratch").exists()
    assert (run_root / "evidence" / "before.txt").is_file()
    assert store.info()["artifacts"][0]["artifact_id"] == evidence["artifact_id"]
    assert store.doctor() == ()


def test_deliverable_publication_is_lazy_versioned_and_discoverable(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    source = tmp_path / "source"
    source.mkdir()
    (source / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "run.sh").chmod(0o700)
    (source / "README.md").write_text("# Tracker\n")
    store.finish_run(
        run.run_id,
        RunState.SUCCEEDED,
        lease_owner="worker-1",
    )
    published = store.publish_deliverable(
        source,
        "tracker",
        kind="browser-automation",
        description="Reusable visa slot tracker",
        reusable=True,
        entrypoint="run.sh",
        produced_by_run=run.run_id,
    )
    root = tmp_path / "tasks" / "visa-slot-tracker"
    assert published.revision == 1
    assert (root / "deliverables" / "tracker" / "run.sh").is_file()
    assert "Reusable visa slot tracker" in (root / "README.md").read_text()
    assert TaskStore(tmp_path).deliverables("visa-slot-tracker") == (published,)
    assert TaskStore(tmp_path).find("finished visa tracker") == (store.load(),)

    (source / "README.md").write_text("# Tracker revision two\n")
    updated = store.publish_deliverable(
        source,
        "tracker",
        kind="browser-automation",
        description="Reusable visa slot tracker",
        reusable=True,
        entrypoint="run.sh",
        produced_by_run=run.run_id,
    )
    assert updated.revision == 2
    assert (root / ".task" / "versions" / "tracker" / "r1").is_dir()


def test_user_modified_deliverable_is_not_overwritten(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("original")
    store.publish_deliverable(
        source,
        "report.md",
        kind="report",
        description="Research report",
    )
    target = (
        tmp_path
        / "tasks"
        / "visa-slot-tracker"
        / "deliverables"
        / "report.md"
    )
    target.write_text("user edit")
    source.write_text("agent replacement")
    with pytest.raises(ValueError, match="modified by the user"):
        store.publish_deliverable(
            source,
            "report.md",
            kind="report",
            description="Research report",
        )
    assert target.read_text() == "user edit"
    assert "modified deliverable: report.md" in store.doctor()


def test_deliverable_source_rejects_symlinks(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (source / "linked.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe entry"):
        store.publish_deliverable(
            source,
            "bundle",
            kind="bundle",
            description="Unsafe bundle",
        )
    assert not (store.path() / "deliverables").exists()


def test_completion_requires_summary_terminal_runs_and_intact_outputs(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    with pytest.raises(ValueError, match="terminal"):
        store.complete("Tracker is ready")
    store.finish_run(
        run.run_id,
        RunState.SUCCEEDED,
        lease_owner="worker-1",
    )
    task = store.complete("Tracker is ready for reuse")
    assert task.state is TaskState.COMPLETED
    readme = (store.path() / "README.md").read_text()
    assert "Ready to use" in readme
    assert "Tracker is ready for reuse" in readme


def test_completion_rejects_placeholders_and_unverified_deliverables(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    with pytest.raises(ValueError, match="placeholder"):
        store.complete("Work in progress.")
    source = tmp_path / "draft.md"
    source.write_text("draft")
    store.publish_deliverable(
        source,
        "draft.md",
        kind="report",
        description="Draft report",
        verified=False,
    )
    with pytest.raises(ValueError, match="verified deliverable"):
        store.complete("The draft report has been produced")


def test_archive_is_metadata_and_supersession_links_replacement(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    TaskStore(tmp_path).create(
        "visa-slot-monitor",
        "Replace the tracker with a maintained monitor",
    )
    store.complete("Original tracker remains available")
    store.archive()
    assert store.path().is_dir()
    with pytest.raises(ValueError, match="restore"):
        store.start_run(lease_owner="worker-1")
    assert TaskStore(tmp_path).list_tasks() == (
        TaskStore(tmp_path).load("visa-slot-monitor"),
    )
    assert {item.task_id for item in TaskStore(tmp_path).find("visa slot")} == {
        "visa-slot-tracker",
        "visa-slot-monitor",
    }
    store.restore()
    assert "visa-slot-tracker" in {
        item.task_id for item in TaskStore(tmp_path).list_tasks()
    }
    task = store.supersede("visa-slot-monitor")
    assert task.state is TaskState.SUPERSEDED
    assert "visa-slot-monitor" in (store.path() / "README.md").read_text()


def test_doctor_detects_missing_deliverable_without_deleting_record(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("report")
    store.publish_deliverable(
        source,
        "report.md",
        kind="report",
        description="Report",
    )
    (store.path() / "deliverables" / "report.md").unlink()
    assert "missing deliverable: report.md" in store.doctor()
    assert store.deliverables()[0].name == "report.md"


def test_doctor_detects_unregistered_visible_output(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    deliverables = store.path() / "deliverables"
    deliverables.mkdir()
    (deliverables / "orphan.txt").write_text("orphan")
    assert "unregistered deliverable: orphan.txt" in store.doctor()


def test_task_store_rejects_tampered_identity(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    database = store.path() / ".task" / "state.sqlite"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT value_json FROM metadata WHERE key = 'task'"
        ).fetchone()
        data = json.loads(row[0])
        data["task_id"] = "other-workspace"
        connection.execute(
            "UPDATE metadata SET value_json = ? WHERE key = 'task'",
            (json.dumps(data),),
        )
    with pytest.raises(ValueError, match="identity"):
        store.load()


def test_routing_is_deterministic_and_disclosure_gated() -> None:
    local = assess(
        RoutingInput(
            deterministic=True,
            dependent_steps=2,
            local_test_decides=True,
            provider_available=True,
        )
    )
    assert local.decision == "local"
    blocked = assess(
        RoutingInput(
            architecture=True,
            dependent_steps=12,
            safety_review=True,
            relevant_files=8,
            provider_available=True,
            disclosure_authorized=False,
        )
    )
    assert blocked.decision == "blocked"
    assert "disclosure not authorized" in blocked.blocked_reasons
    delegated = assess(
        RoutingInput(
            architecture=True,
            dependent_steps=12,
            safety_review=True,
            relevant_files=8,
            provider_available=True,
            disclosure_authorized=True,
        )
    )
    assert delegated.decision == "delegate"
    assert delegated.provider == "chatgpt-web"
    assert delegated.transport == "surf-ui"


def test_routing_research_modes_fail_closed() -> None:
    standard = assess(
        RoutingInput(
            web_research=True,
            current_information=True,
            cross_source_synthesis=True,
            disclosure_authorized=True,
        )
    )
    assert standard.research_mode == "standard"
    deep = assess(
        RoutingInput(
            web_research=True,
            current_information=True,
            cross_source_synthesis=True,
            large_research_volume=True,
            disclosure_authorized=True,
        )
    )
    assert deep.research_mode == "deep"
    unavailable = assess(
        RoutingInput(
            web_research=True,
            regulatory=True,
            large_research_volume=True,
            deep_research_available=False,
            disclosure_authorized=True,
        )
    )
    assert unavailable.decision == "blocked"
    wrong_transport = assess(
        RoutingInput(
            web_research=True,
            disclosure_authorized=True,
            requested_transport="in-app-browser",
        )
    )
    assert wrong_transport.decision == "blocked"


def test_scanner_finds_secrets_binary_task_and_archive_material(
    tmp_path: Path,
) -> None:
    source = tmp_path / "safe.txt"
    source.write_text("api_key = 'abcdefghijklmnopqrstuvwxyz123456'")
    assert any(item.kind == "api_token" for item in scan_file(tmp_path, source))
    binary = tmp_path / "image.bin"
    binary.write_bytes(b"a\0b")
    assert any(item.kind == "binary_denied" for item in scan_file(tmp_path, binary))
    for directory in ("tasks", "archive"):
        material = tmp_path / directory / "one" / "notes.md"
        material.parent.mkdir(parents=True)
        material.write_text("notes")
        assert any(
            finding.kind == "task_material"
            for finding in scan_file(tmp_path, material)
        )


def test_scanner_rejects_parent_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert any(
        finding.kind == "unsupported_type"
        for finding in scan_file(root, root / "link" / "secret.txt")
    )


def test_delegation_is_bound_to_provider_context_and_request() -> None:
    request = DelegationRequest(
        "visa-slot-tracker",
        "req",
        "chatgpt-web",
        "abc",
        "review",
    )
    decision = DisclosureDecision(
        "d",
        "visa-slot-tracker",
        "chatgpt-web",
        "abc",
        ("src",),
        "approved",
    )
    validate_disclosure(decision, request)
    response = DelegationResponse(
        "visa-slot-tracker",
        "req",
        "chatgpt-web",
        "abc",
        "review",
        {"verdict": "ready"},
    )
    validate_response(request, response)
    with pytest.raises(ValueError):
        validate_response(
            request,
            DelegationResponse(
                "visa-slot-tracker",
                "other",
                "chatgpt-web",
                "abc",
                "review",
                {},
            ),
        )


def test_delegation_request_is_bound_to_task_policy(tmp_path: Path) -> None:
    task = TaskStore(tmp_path).create("visa-slot-tracker", "Goal")
    request = DelegationRequest(
        task.task_id,
        "req",
        "chatgpt-web",
        "abc",
        "research",
        research_mode="deep",
    )
    validate_request_policy(task, request)
    with pytest.raises(PermissionError, match="transport"):
        validate_request_policy(
            task,
            DelegationRequest(
                task.task_id,
                "req",
                "chatgpt-web",
                "abc",
                "research",
                transport="api",
            ),
        )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeAdapter:
    """Adapter that captures its post-action evidence into the run.

    A verified consequential result must cite the digest of evidence that is
    actually stored, so an adapter has to persist what it observed rather than
    return an unanchored digest.
    """

    adapter_id = "surf:fake"

    def __init__(self, store: TaskStore | None = None, run_id: str = "", lease_owner: str = ""):
        self.store = store
        self.run_id = run_id
        self.lease_owner = lease_owner
        self.captures = 0

    def capabilities(self):
        return None

    def claim(self, task_id):
        return ("tab-1",)

    def observe(self, task_id, target):
        return BrowserObservation(
            task_id, None, target, {}, self._store_evidence("pre"), "tab-1"
        )

    def act(self, action):
        return BrowserObservation(
            action.task_id,
            action.action_id,
            action.target,
            {"sent": "yes"},
            self._store_evidence("post"),
            "tab-1",
        )

    def capture(self, task_id, action_id):
        return BrowserObservation(
            task_id, action_id, "", {}, self._store_evidence("capture"), "tab-1"
        )

    def _store_evidence(self, label: str) -> str:
        if self.store is None:
            return digest(label)
        self.captures += 1
        name = f"{label}-{self.captures}.txt"
        source = self.store.path().parent.parent / name
        source.write_text(f"observed {label} {self.captures}", encoding="utf-8")
        stored = self.store.store_run_artifact(
            self.run_id,
            "evidence",
            source,
            name,
            lease_owner=self.lease_owner,
        )
        return stored["sha256"]


class ForbiddenAdapter(FakeAdapter):
    adapter_id = "in-app-browser:fake"


class FailingAdapter(FakeAdapter):
    def act(self, action):
        raise RuntimeError("transport disappeared after submission")


def test_orchestrator_rejects_non_surf_adapter(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    with pytest.raises(PermissionError, match="forbidden"):
        Orchestrator(
            store,
            "visa-slot-tracker",
            ForbiddenAdapter(),
            run_id=run.run_id,
            lease_owner="worker-1",
        )


def test_orchestrator_blocks_then_consumes_exact_grant(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    action = BrowserAction(
        "a",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        "https://example.test",
        "send message",
        ({"type": "state_equals", "key": "sent", "value": "yes"},),
    )
    orchestrator = Orchestrator(
        store,
        "visa-slot-tracker",
        FakeAdapter(store, run.run_id, "worker-1"),
        run_id=run.run_id,
        lease_owner="worker-1",
    )
    with pytest.raises(PermissionError):
        orchestrator.execute(action)
    grant = AuthorizationGrant(
        "g",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        action.target,
        summary_sha256(action),
        "2999-01-01T00:00:00+00:00",
    )
    result = orchestrator.execute(action, grant)
    assert result.outcome == "verified"
    assert result.consumed_grant and result.consumed_grant.uses == 1
    with pytest.raises(ValueError, match="uses"):
        orchestrator.execute(action, grant)
    store.finish_run(
        run.run_id,
        RunState.SUCCEEDED,
        lease_owner="worker-1",
    )


def test_consequential_transport_failure_becomes_unknown_outcome(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    action = BrowserAction(
        "commit-1",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        "https://example.test",
        "submit request",
        ({"type": "state_equals", "key": "sent", "value": "yes"},),
    )
    grant = AuthorizationGrant(
        "grant-1",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        action.target,
        summary_sha256(action),
        "2999-01-01T00:00:00+00:00",
    )
    orchestrator = Orchestrator(
        store,
        "visa-slot-tracker",
        FailingAdapter(store, run.run_id, "worker-1"),
        run_id=run.run_id,
        lease_owner="worker-1",
    )
    with pytest.raises(RuntimeError, match="transport disappeared"):
        orchestrator.execute(action, grant)
    unresolved = store.unresolved_actions()
    assert unresolved[0]["action_id"] == "commit-1"
    assert unresolved[0]["status"] == "OUTCOME_UNKNOWN"
    assert "unresolved action: commit-1 (OUTCOME_UNKNOWN)" in store.doctor()
    capture = tmp_path / "capture.txt"
    capture.write_text("no submission is visible in the account", encoding="utf-8")
    evidence = store.store_run_artifact(
        run.run_id, "evidence", capture, "capture.txt",
        lease_owner="worker-1",
    )
    store.finish_run(
        run.run_id,
        RunState.FAILED,
        lease_owner="worker-1",
        failure="Transport disappeared after submission",
        failure_class="transport_lost",
        recoverable=True,
    )
    with pytest.raises(ValueError, match="reconciled"):
        store.complete("Request was submitted")
    store.reconcile_action(
        "commit-1",
        verified=False,
        evidence_sha256=evidence["sha256"],
    )
    assert store.unresolved_actions() == ()
    assert store.complete("The request was not submitted").state is TaskState.COMPLETED


def test_content_hash_is_not_wildcard() -> None:
    action = BrowserAction(
        "a",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        "target",
        "send",
        (),
        "payload",
    )
    grant = AuthorizationGrant(
        "g",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        "target",
        summary_sha256(action),
        "2999-01-01T00:00:00+00:00",
    )
    from browser_tasks.authorization import validate_grant

    with pytest.raises(ValueError, match="content"):
        validate_grant(grant, action)


def test_consequential_action_policy() -> None:
    assert not requires_authorization(ActionClass.OBSERVE)
    assert requires_authorization(ActionClass.COMMIT_EXTERNAL)
    assert retry_policy(ActionClass.COMMIT_EXTERNAL, "ambiguous") == "block"


def commit_action(
    action_id: str = "commit-1", *, target: str = "https://example.test"
) -> BrowserAction:
    return BrowserAction(
        action_id,
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        target,
        "submit request",
        ({"type": "state_equals", "key": "sent", "value": "yes"},),
    )


def bound_adapter(store: TaskStore) -> None:
    store.bind_adapter("surf:fake", ("tab-1",))


def install_grant_for(
    store: TaskStore,
    action: BrowserAction,
    grant_id: str = "grant-1",
    *,
    max_uses: int = 1,
) -> AuthorizationGrant:
    grant = AuthorizationGrant(
        grant_id,
        action.task_id,
        action.action_class,
        action.target,
        summary_sha256(action),
        "2999-01-01T00:00:00+00:00",
        max_uses=max_uses,
        content_sha256=action.content_sha256,
    )
    store.install_grant(grant)
    return grant


def test_execution_requires_the_current_unexpired_owned_lease(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1", lease_seconds=10)
    bound_adapter(store)
    action = commit_action()
    grant = install_grant_for(store, action)

    with pytest.raises(PermissionError, match="another worker"):
        store.reserve_execution(
            action, grant.grant_id, run_id=run.run_id, lease_owner="worker-2"
        )
    with pytest.raises(PermissionError, match="current run"):
        store.reserve_execution(
            action,
            grant.grant_id,
            run_id="not-the-current-run",
            lease_owner="worker-1",
        )

    expired = datetime.now(UTC) - timedelta(seconds=1)
    with sqlite3.connect(store.path() / ".task" / "state.sqlite") as connection:
        connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            (expired.isoformat(timespec="microseconds"), run.run_id),
        )
    with pytest.raises(ValueError, match="lease has expired"):
        store.reserve_execution(
            action, grant.grant_id, run_id=run.run_id, lease_owner="worker-1"
        )
    assert store.unresolved_actions() == ()
    # No rejected reservation consumed the single-use grant: renewing the lease
    # and reserving properly still works.
    store.heartbeat(run.run_id, lease_owner="worker-1")
    consumed = store.reserve_execution(
        action, grant.grant_id, run_id=run.run_id, lease_owner="worker-1"
    )
    assert consumed is not None and consumed.uses == 1


def test_owner_can_finish_a_run_whose_lease_expired_unrecovered(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1", lease_seconds=10)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    with sqlite3.connect(store.path() / ".task" / "state.sqlite") as connection:
        connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            (expired.isoformat(timespec="microseconds"), run.run_id),
        )
    finished = store.finish_run(
        run.run_id, RunState.SUCCEEDED, lease_owner="worker-1"
    )
    assert finished.state is RunState.SUCCEEDED
    types = {event["event_type"] for event in store.audit_events()}
    assert "run.finished_after_lease_expiry" in types


def test_lease_seconds_are_bounded(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    with pytest.raises(ValueError, match="at least"):
        store.start_run(lease_owner="worker-1", lease_seconds=5)
    with pytest.raises(ValueError, match="exceed"):
        store.start_run(lease_owner="worker-1", lease_seconds=10**9)


def test_abandon_run_releases_a_wedged_workspace(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="ghost", lease_seconds=21600)
    assert store.recover_expired_runs() == ()
    store.abandon_run(run.run_id, "worker host was reimaged")
    assert store.start_run(lease_owner="worker-2").run_id != run.run_id


def test_consequential_action_requires_supported_postconditions(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    bound_adapter(store)
    bare = BrowserAction(
        "commit-bare",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        "https://example.test",
        "submit request",
        (),
    )
    bare_grant = install_grant_for(store, bare, "grant-bare")
    with pytest.raises(ValueError, match="at least one postcondition"):
        store.reserve_execution(
            bare,
            bare_grant.grant_id,
            run_id=run.run_id,
            lease_owner="worker-1",
        )
    unsupported = BrowserAction(
        "commit-unsupported",
        "visa-slot-tracker",
        ActionClass.COMMIT_EXTERNAL,
        "https://example.test",
        "submit request",
        ({"type": "vibes_match", "value": "good"},),
    )
    unsupported_grant = install_grant_for(store, unsupported, "grant-unsupported")
    with pytest.raises(ValueError, match="supported checks"):
        store.reserve_execution(
            unsupported,
            unsupported_grant.grant_id,
            run_id=run.run_id,
            lease_owner="worker-1",
        )
    assert verify(bare, FakeAdapter().act(bare)) == "ambiguous"


def test_consequential_intent_cannot_bypass_authorization(tmp_path: Path) -> None:
    """The documented CLI path must not be the unauthorized one."""

    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    bound_adapter(store)
    action = commit_action()
    with pytest.raises(PermissionError, match="authorization grant"):
        store.record_action_intent(
            action, run_id=run.run_id, lease_owner="worker-1"
        )
    with pytest.raises(PermissionError, match="authorization grant"):
        store.reserve_execution(
            action, None, run_id=run.run_id, lease_owner="worker-1"
        )
    assert store.unresolved_actions() == ()
    root = ["--root", str(tmp_path)]
    contract = [
        "--action-class",
        "financial",
        "--target",
        "https://bank.example/transfer",
        "--summary",
        "wire 5000 EUR",
        "--postcondition",
        '{"type": "url_equals", "value": "https://bank.example/done"}',
    ]
    assert (
        cli_main(
            [
                *root,
                "task",
                "action-intent",
                "visa-slot-tracker",
                "wire-1",
                "--run-id",
                run.run_id,
                "--lease-owner",
                "worker-1",
                *contract,
            ]
        )
        == 2
    )
    assert store.unresolved_actions() == ()


def test_verified_consequential_result_requires_evidence(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    bound_adapter(store)
    action = commit_action()
    grant = install_grant_for(store, action)
    store.reserve_execution(
        action, grant.grant_id, run_id=run.run_id, lease_owner="worker-1"
    )
    with pytest.raises(ValueError, match="observed evidence"):
        store.record_action_result(
            action.action_id,
            "verified",
            run_id=run.run_id,
            lease_owner="worker-1",
        )
    with pytest.raises(ValueError, match="sha256 digest"):
        store.record_action_result(
            action.action_id,
            "verified",
            "looks-fine",
            run_id=run.run_id,
            lease_owner="worker-1",
        )
    # A well-formed digest that matches nothing captured is still fabricated.
    with pytest.raises(ValueError, match="stored evidence"):
        store.record_action_result(
            action.action_id,
            "verified",
            digest("never captured"),
            run_id=run.run_id,
            lease_owner="worker-1",
        )
    capture = tmp_path / "after.txt"
    capture.write_text("the booking is visible", encoding="utf-8")
    stored = store.store_run_artifact(
        run.run_id, "evidence", capture, "after.txt", lease_owner="worker-1"
    )
    assert (
        store.record_action_result(
            action.action_id,
            "verified",
            stored["sha256"],
            run_id=run.run_id,
            lease_owner="worker-1",
        )
        == "VERIFIED"
    )


def test_reconciliation_requires_a_captured_artifact_digest(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    bound_adapter(store)
    action = commit_action()
    grant = install_grant_for(store, action)
    store.reserve_execution(
        action, grant.grant_id, run_id=run.run_id, lease_owner="worker-1"
    )
    store.record_action_result(
        action.action_id,
        "ambiguous",
        run_id=run.run_id,
        lease_owner="worker-1",
    )
    with pytest.raises(ValueError, match="sha256 digest"):
        store.reconcile_action(
            action.action_id, verified=True, evidence_sha256="observed"
        )
    with pytest.raises(ValueError, match="stored evidence"):
        store.reconcile_action(
            action.action_id, verified=True, evidence_sha256=digest("unstored")
        )


def test_equivalent_unresolved_consequential_action_is_blocked(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    bound_adapter(store)
    first = commit_action("commit-1")
    first_grant = install_grant_for(store, first, "grant-first")
    store.reserve_execution(
        first, first_grant.grant_id, run_id=run.run_id, lease_owner="worker-1"
    )
    store.record_action_result(
        first.action_id,
        "ambiguous",
        run_id=run.run_id,
        lease_owner="worker-1",
    )
    retry = commit_action("commit-2")
    retry_grant = install_grant_for(store, retry, "grant-retry")
    with pytest.raises(ValueError, match="unresolved"):
        store.reserve_execution(
            retry, retry_grant.grant_id, run_id=run.run_id, lease_owner="worker-1"
        )


def test_grant_binds_the_postconditions(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    bound_adapter(store)
    strict = commit_action()
    relaxed = BrowserAction(
        strict.action_id,
        strict.task_id,
        strict.action_class,
        strict.target,
        strict.summary,
        ({"type": "url_equals", "value": "https://example.test/anything"},),
    )
    assert summary_sha256(strict) != summary_sha256(relaxed)
    # A grant approved for the strict contract cannot be spent on the weaker
    # one, even though target, class and summary text are identical.
    grant = install_grant_for(store, strict)
    with pytest.raises(ValueError, match="summary"):
        store.reserve_execution(
            relaxed, grant.grant_id, run_id=run.run_id, lease_owner="worker-1"
        )


def test_recovery_prunes_scratch_of_an_interrupted_run(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1", lease_seconds=10)
    scratch_source = tmp_path / "scratch.txt"
    scratch_source.write_text("intermediate", encoding="utf-8")
    stored = store.store_run_artifact(
        run.run_id, "scratch", scratch_source, "scratch.txt",
        lease_owner="worker-1",
    )
    assert (store.path() / stored["path"]).is_file()
    recovered = store.recover_expired_runs(
        now=datetime.now(UTC) + timedelta(seconds=30)
    )
    assert recovered == (run.run_id,)
    assert not (store.path() / stored["path"]).exists()
    assert store.doctor() == ()


def test_repair_prunes_scratch_left_on_a_terminal_run(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    source = tmp_path / "scratch.txt"
    source.write_text("intermediate", encoding="utf-8")
    stored = store.store_run_artifact(
        run.run_id, "scratch", source, "scratch.txt",
        lease_owner="worker-1",
    )
    # Terminate the run without going through finish_run, as a crash would.
    with sqlite3.connect(store.path() / ".task" / "state.sqlite") as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', lease_owner = NULL, "
            "lease_expires_at = NULL WHERE run_id = ?",
            (run.run_id,),
        )
        connection.execute(
            "UPDATE metadata SET value_json = 'null' WHERE key = 'current_run_id'"
        )
    assert any("scratch survives terminal run" in issue for issue in store.doctor())
    # Exercises the nested filesystem lock: repair holds it and discards scratch.
    assert any("pruned scratch" in item for item in store.repair()["actions"])
    assert not (store.path() / stored["path"]).exists()
    assert store.doctor() == ()

    # The same must hold for an interrupted run, which is the common crash shape.
    second = store.start_run(lease_owner="worker-2")
    stored = store.store_run_artifact(
        second.run_id, "scratch", source, "scratch.txt", lease_owner="worker-2"
    )
    with sqlite3.connect(store.path() / ".task" / "state.sqlite") as connection:
        connection.execute(
            "UPDATE runs SET state = 'INTERRUPTED', lease_owner = NULL, "
            "lease_expires_at = NULL WHERE run_id = ?",
            (second.run_id,),
        )
        connection.execute(
            "UPDATE metadata SET value_json = 'null' WHERE key = 'current_run_id'"
        )
    assert any("pruned scratch" in item for item in store.repair()["actions"])
    assert not (store.path() / stored["path"]).exists()


def crash_before_artifact_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate power loss between the visible rename and the row insert.

    `BaseException` skips the `except Exception` cleanup, so what remains on disk
    is what a crash would leave rather than what the code tidied up.
    """

    def die(self, connection, pending):
        raise BaseException("power loss before the artifact row was written")

    monkeypatch.setattr(TaskStore, "_insert_artifact_row", die)


def test_interrupted_artifact_store_is_repairable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    first = tmp_path / "before.txt"
    first.write_text("pre-action state", encoding="utf-8")

    crash_before_artifact_row(monkeypatch)
    with pytest.raises(BaseException, match="power loss"):
        store.store_run_artifact(
            run.run_id, "evidence", first, "before.txt", lease_owner="worker-1"
        )
    monkeypatch.undo()

    orphan = store.path() / ".task" / "runs" / run.run_id / "evidence" / "before.txt"
    assert orphan.is_file()
    assert store.info()["artifacts"] == []
    assert any("interrupted run artifact" in issue for issue in store.doctor())

    # A second store must resolve the pending entry rather than overwrite it:
    # overwriting stranded the first file with no record of it anywhere.
    second = tmp_path / "after.txt"
    second.write_text("post-action state", encoding="utf-8")
    store.store_run_artifact(
        run.run_id, "evidence", second, "after.txt", lease_owner="worker-1"
    )
    registered = {item["path"] for item in store.info()["artifacts"]}
    assert orphan.relative_to(store.path()).as_posix() in registered
    assert store.doctor() == ()


def test_orphan_artifact_without_a_journal_entry_is_repairable(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1")
    source = tmp_path / "before.txt"
    source.write_text("pre-action state", encoding="utf-8")
    stored = store.store_run_artifact(
        run.run_id, "evidence", source, "before.txt", lease_owner="worker-1"
    )
    # A file under .task/runs with neither a row nor a journal entry used to
    # block completion forever with no command able to clear it.
    with sqlite3.connect(store.path() / ".task" / "state.sqlite") as connection:
        connection.execute(
            "DELETE FROM run_artifacts WHERE artifact_id = ?",
            (stored["artifact_id"],),
        )
    assert any("unregistered run artifact" in issue for issue in store.doctor())
    assert any(
        "adopted orphan run artifact" in item
        for item in store.repair()["actions"]
    )
    assert store.doctor() == ()


def test_publish_crash_before_registration_is_repairable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("first revision", encoding="utf-8")
    original = TaskStore._transaction
    calls = {"count": 0}

    def flaky(self):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("process died before the metadata commit")
        return original(self)

    monkeypatch.setattr(TaskStore, "_transaction", flaky)
    with pytest.raises(RuntimeError, match="process died"):
        store.publish_deliverable(
            source, "report.md", kind="report", description="Findings"
        )
    monkeypatch.undo()

    visible = store.path() / "deliverables" / "report.md"
    assert visible.is_file()
    assert store.deliverables() == ()
    assert any("interrupted publish" in issue for issue in store.doctor())

    actions = store.repair()["actions"]
    assert any("completed interrupted publish" in item for item in actions)
    published = store.deliverables()
    assert published and published[0].name == "report.md"
    assert store.doctor() == ()

    # The recovered state must not be mistaken for a user edit afterwards.
    source.write_text("second revision", encoding="utf-8")
    assert (
        store.publish_deliverable(
            source, "report.md", kind="report", description="Findings"
        ).revision
        == 2
    )


def test_publish_never_overwrites_an_existing_revision_backup(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "report.md"
    for index, text in enumerate(("one", "two", "three"), start=1):
        source.write_text(text, encoding="utf-8")
        assert (
            store.publish_deliverable(
                source, "report.md", kind="report", description="Findings"
            ).revision
            == index
        )
    versions = store.path() / ".task" / "versions" / "report.md"
    assert {item.read_text(encoding="utf-8") for item in versions.iterdir()} == {
        "one",
        "two",
    }
    # Force the collision branch: the next publish would back up to r3, which
    # already exists, and must not overwrite it.
    (versions / "r3").write_text("kept revision", encoding="utf-8")
    source.write_text("four", encoding="utf-8")
    assert (
        store.publish_deliverable(
            source, "report.md", kind="report", description="Findings"
        ).revision
        == 4
    )
    assert (versions / "r3").read_text(encoding="utf-8") == "kept revision"
    assert "three" in {
        item.read_text(encoding="utf-8") for item in versions.iterdir()
    }


def test_entrypoint_must_stay_inside_the_deliverable(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (bundle / "run.sh").chmod(0o700)
    for escape in ("/bin/sh", "../../../bin/sh"):
        with pytest.raises(ValueError, match="relative in-tree path"):
            store.publish_deliverable(
                bundle,
                "tracker",
                kind="script",
                description="Runner",
                reusable=True,
                entrypoint=escape,
            )
    with pytest.raises(ValueError, match="safe path components"):
        store.publish_deliverable(
            bundle,
            "tracker",
            kind="script",
            description="Runner",
            reusable=True,
            entrypoint="sub dir/run.sh",
        )
    with pytest.raises(ValueError, match="missing or unsafe"):
        store.publish_deliverable(
            bundle,
            "tracker",
            kind="script",
            description="Runner",
            reusable=True,
            entrypoint="absent.sh",
        )
    published = store.publish_deliverable(
        bundle,
        "tracker",
        kind="script",
        description="Runner",
        reusable=True,
        entrypoint="run.sh",
    )
    assert published.entrypoint == "run.sh"


def test_single_file_reusable_script_can_be_published(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o700)
    published = store.publish_deliverable(
        script,
        "check.sh",
        kind="script",
        description="Standalone check",
        reusable=True,
        entrypoint="check.sh",
    )
    assert published.reusable and published.entrypoint == "check.sh"
    plain = tmp_path / "notes.sh"
    plain.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    plain.chmod(0o600)
    with pytest.raises(ValueError, match="not executable"):
        store.publish_deliverable(
            plain,
            "notes.sh",
            kind="script",
            description="Not runnable",
            reusable=True,
            entrypoint="notes.sh",
        )


def test_published_directory_mode_change_is_detected(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "data.txt").write_text("payload", encoding="utf-8")
    store.publish_deliverable(
        bundle, "bundle", kind="dataset", description="Rows"
    )
    (store.path() / "deliverables" / "bundle").chmod(0o750)
    assert any("modified deliverable" in issue for issue in store.doctor())


def test_bare_timestamp_slug_is_rejected_on_every_path(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    with pytest.raises(ValueError, match="semantic name"):
        store.create("20260725-093000", "Legacy shaped workspace")
    legacy = tmp_path / "tasks" / "20260725-093000"
    (legacy / ".task").mkdir(parents=True)
    (legacy / ".task" / "state.sqlite").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="semantic name"):
        store.bind("20260725-093000")
    assert [item["task_id"] for item in store.damaged_workspaces()] == [
        "20260725-093000"
    ]
    assert store.list_tasks() == ()


def test_completion_rejects_publish_staging_and_archived_state(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    store.set_outcome("The tracker is published and verified")
    staging = store.path() / ".task" / ".publish-orphan"
    staging.mkdir()
    with pytest.raises(ValueError, match="publish staging"):
        store.complete("The tracker is published and verified")
    assert any(
        "removed abandoned publish staging" in item
        for item in store.repair()["actions"]
    )
    assert not staging.exists()
    store.archive()
    with pytest.raises(ValueError, match="restore the workspace"):
        store.complete("The tracker is published and verified")


def test_repair_regenerates_a_stale_readme(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    readme = store.path() / "README.md"
    readme.write_text("# stale\n", encoding="utf-8")
    assert any("README" in issue for issue in store.doctor())
    assert any("README" in item for item in store.repair()["actions"])
    assert store.doctor() == ()


def test_readme_cannot_be_forged_through_free_text(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("payload", encoding="utf-8")
    store.publish_deliverable(
        source,
        "report.md",
        kind="report",
        description="Findings\n\n**Status:** Ready to use",
    )
    lines = (store.path() / "README.md").read_text(encoding="utf-8").splitlines()
    # The text survives as content but cannot become its own status line.
    assert "**Status:** Ready to use" not in lines
    assert any("Findings **Status:** Ready to use" in line for line in lines)
    assert "**Status:** Draft" in lines


def test_audit_chain_detects_a_rewritten_event(tmp_path: Path) -> None:
    store = create_task(tmp_path)
    store.set_outcome("The tracker is published and verified")
    assert store.doctor() == ()
    with sqlite3.connect(store.path() / ".task" / "state.sqlite") as connection:
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE sequence = 1",
            (json.dumps({"task_id": "visa-slot-tracker", "tampered": True}),),
        )
    assert any("audit chain broken" in issue for issue in store.doctor())


def test_browser_resources_are_exclusive_across_workspaces(
    tmp_path: Path,
) -> None:
    first = create_task(tmp_path)
    second = create_task(tmp_path, "flight-price-watch")
    first.bind_adapter("surf:fake", ("tab-1",))
    with pytest.raises(PermissionError, match="owned by"):
        second.bind_adapter("surf:fake", ("tab-1",))
    second.bind_adapter("surf:fake", ("tab-2",))
    # A completed workspace must not burn the tab it used forever.
    first.set_outcome("The tracker is published and verified")
    first.complete("The tracker is published and verified")
    # Rebinding replaces the claim set rather than accumulating it.
    second.bind_adapter("surf:fake", ("tab-1",))
    assert second.release_browser_resources() == ("tab-1",)
    assert second.release_browser_resources() == ()


def test_find_requires_more_than_a_four_character_prefix(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    store.create("trackpad-firmware", "Investigate trackpad firmware")
    assert [item.task_id for item in store.find("trackpad")] == [
        "trackpad-firmware"
    ]
    assert store.find("tracker") == ()


def test_find_works_from_a_bound_store(tmp_path: Path) -> None:
    create_task(tmp_path)
    TaskStore(tmp_path).create("flight-price-watch", "Watch airfare changes")
    bound = TaskStore(tmp_path).bind("flight-price-watch")
    assert [item.task_id for item in bound.find("visa slot")] == [
        "visa-slot-tracker"
    ]


def test_scanner_allows_env_example_and_denies_key_material(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    assert scan_file(tmp_path, tmp_path / ".env.example") == ()
    (tmp_path / "id_ed25519").write_text("private material\n", encoding="utf-8")
    assert any(
        finding.kind == "denied_name"
        for finding in scan_file(tmp_path, tmp_path / "id_ed25519")
    )
    (tmp_path / "NOTES.MD").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8"
    )
    assert any(
        finding.kind == "private_key"
        for finding in scan_file(tmp_path, tmp_path / "NOTES.MD")
    )


def test_scanner_excludes_are_case_insensitive(tmp_path: Path) -> None:
    target = tmp_path / "TASKS" / "notes.md"
    target.parent.mkdir()
    target.write_text("evidence", encoding="utf-8")
    assert any(
        finding.kind == "task_material"
        for finding in scan_file(tmp_path, target)
    )


def test_cli_doctor_json_exit_code_and_error_mapping(tmp_path: Path) -> None:
    assert (
        cli_main(
            [
                "--root",
                str(tmp_path),
                "task",
                "init",
                "visa-slot-tracker",
                "--goal",
                "Build a reusable visa slot tracker",
            ]
        )
        == 0
    )
    store = TaskStore(tmp_path).bind("visa-slot-tracker")
    source = tmp_path / "report.md"
    source.write_text("payload", encoding="utf-8")
    store.publish_deliverable(
        source, "report.md", kind="report", description="Findings"
    )
    (store.path() / "deliverables" / "report.md").write_text(
        "edited by hand", encoding="utf-8"
    )
    root = ["--root", str(tmp_path)]
    assert cli_main([*root, "task", "doctor", "visa-slot-tracker"]) == 2
    assert cli_main([*root, "task", "doctor", "visa-slot-tracker", "--json"]) == 2
    assert cli_main([*root, "task", "status", "no-such-workspace"]) == 4
    assert (
        cli_main(
            [
                *root,
                "task",
                "init",
                "visa-slot-tracker",
                "--goal",
                "Duplicate workspace",
            ]
        )
        == 3
    )
    assert cli_main([*root, "guard", "no-such-workspace", "--capability", "browser", "--tool", "surf"]) == 4
    assert cli_main([*root, "task", "outcome", "visa-slot-tracker", "--summary", "todo"]) == 2


def test_doctor_json_body_and_expired_lease_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = create_task(tmp_path)
    run = store.start_run(lease_owner="worker-1", lease_seconds=10)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    with sqlite3.connect(store.path() / ".task" / "state.sqlite") as connection:
        connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            (expired.isoformat(timespec="microseconds"), run.run_id),
        )
    assert (
        cli_main(
            ["--root", str(tmp_path), "task", "doctor", "visa-slot-tracker", "--json"]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == "visa-slot-tracker"
    # The machine-readable body must actually carry the issue, not just the code.
    assert any(
        "expired lease on active run" in issue for issue in payload["issues"]
    )


def test_completion_requires_an_explicitly_verified_deliverable(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("findings", encoding="utf-8")
    published = store.publish_deliverable(
        source, "report.md", kind="report", description="Findings"
    )
    # Publication does not assert verification on the operator's behalf.
    assert published.verified is False
    with pytest.raises(ValueError, match="verified deliverable"):
        store.complete("The findings are published and ready to reuse")
    store.publish_deliverable(
        source,
        "report.md",
        kind="report",
        description="Findings",
        verified=True,
    )
    assert (
        store.complete("The findings are published and ready to reuse").state
        is TaskState.COMPLETED
    )


def test_repair_separates_diagnostics_from_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = create_task(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("first revision", encoding="utf-8")
    original = TaskStore._transaction
    calls = {"count": 0}

    def flaky(self):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("process died before the metadata commit")
        return original(self)

    monkeypatch.setattr(TaskStore, "_transaction", flaky)
    with pytest.raises(RuntimeError, match="process died"):
        store.publish_deliverable(
            source, "report.md", kind="report", description="Findings"
        )
    monkeypatch.undo()
    # A user edit on top of the crashed publish leaves a state repair cannot
    # decide by itself; it must report that rather than claim a repair.
    (store.path() / "deliverables" / "report.md").write_text(
        "edited by hand", encoding="utf-8"
    )
    outcome = store.repair()
    assert outcome["actions"] == ()
    assert any("unresolved publish journal" in item for item in outcome["diagnostics"])
    assert "workspace.repaired" not in {
        event["event_type"] for event in store.audit_events()
    }
    root = ["--root", str(tmp_path)]
    assert cli_main([*root, "task", "repair", "visa-slot-tracker"]) == 2
    # The operator has an explicit, audited way out.
    assert (
        cli_main([*root, "task", "repair", "visa-slot-tracker", "--adopt-visible"])
        == 0
    )
    assert store.doctor() == ()
    assert (
        store.path() / "deliverables" / "report.md"
    ).read_text(encoding="utf-8") == "edited by hand"


def test_cli_guard_denies_archived_and_cancelled_workspaces(
    tmp_path: Path,
) -> None:
    store = create_task(tmp_path)
    root = ["--root", str(tmp_path)]
    guard = [*root, "guard", "visa-slot-tracker", "--capability", "research", "--tool", "web-chat"]
    assert cli_main(guard) == 0
    store.archive()
    assert cli_main(guard) == 2
    store.restore()
    store.cancel("The intent was abandoned")
    assert cli_main(guard) == 2


def test_cli_scan_fails_closed_on_a_secret_in_a_benign_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config.py").write_text(
        'api_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n', encoding="utf-8"
    )
    (repo / "clean.py").write_text("print('ok')\n", encoding="utf-8")
    assert (
        cli_main(["scan", "--repo-root", str(repo), "clean.py"]) == 0
    )
    assert cli_main(["scan", "--repo-root", str(repo), "config.py"]) == 5


def test_cli_exposes_the_execution_lifecycle(tmp_path: Path) -> None:
    root = ["--root", str(tmp_path)]
    assert cli_main([*root, "task", "init", "visa-slot-tracker", "--goal", "Track visa slots"]) == 0
    store = TaskStore(tmp_path).bind("visa-slot-tracker")
    run = store.start_run(lease_owner="worker-1")
    assert (
        cli_main(
            [
                *root,
                "task",
                "bind-adapter",
                "visa-slot-tracker",
                "--adapter",
                "surf:cli",
                "--resource",
                "tab-9",
            ]
        )
        == 0
    )
    contract = [
        "--action-class",
        "commit_external",
        "--target",
        "https://example.test",
        "--summary",
        "submit request",
        "--postcondition",
        '{"type": "url_equals", "value": "https://example.test/done"}',
    ]
    # Authorization comes first: the intent cannot be recorded without it.
    assert (
        cli_main(
            [
                *root,
                "task",
                "grant-install",
                "visa-slot-tracker",
                "--grant-id",
                "grant-1",
                "--expires-at",
                "2999-01-01T00:00:00+00:00",
                *contract,
            ]
        )
        == 0
    )
    assert "authorization.installed" in {
        event["event_type"] for event in store.audit_events()
    }
    assert (
        cli_main(
            [
                *root,
                "task",
                "action-intent",
                "visa-slot-tracker",
                "commit-1",
                "--run-id",
                run.run_id,
                "--lease-owner",
                "worker-1",
                "--grant-id",
                "grant-1",
                *contract,
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                *root,
                "task",
                "action-result",
                "visa-slot-tracker",
                "commit-1",
                "--run-id",
                run.run_id,
                "--lease-owner",
                "worker-1",
                "--outcome",
                "ambiguous",
            ]
        )
        == 0
    )
    assert store.unresolved_actions()[0]["status"] == "OUTCOME_UNKNOWN"
    assert (
        cli_main(
            [
                *root,
                "task",
                "run-state",
                "visa-slot-tracker",
                run.run_id,
                "--state",
                "VERIFYING",
                "--lease-owner",
                "worker-1",
            ]
        )
        == 0
    )
    assert store.runs()[0].state is RunState.VERIFYING
