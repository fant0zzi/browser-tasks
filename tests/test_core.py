from __future__ import annotations

from pathlib import Path

import pytest

from browser_tasks.lifecycle import transition
from browser_tasks.authorization import summary_sha256
from browser_tasks.delegation import validate_disclosure, validate_response
from browser_tasks.models import (
    ActionClass, AuthorizationGrant, BrowserAction, BrowserObservation,
    DelegationRequest, DelegationResponse, DisclosureDecision, RoutingInput, TaskState,
)
from browser_tasks.orchestrator import Orchestrator
from browser_tasks.policy import requires_authorization, retry_policy
from browser_tasks.routing import assess
from browser_tasks.scanner import scan_file
from browser_tasks.task_store import TaskStore


def test_task_store_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    with pytest.raises(ValueError):
        store.path("../escape")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "tasks" / "20260724-010000-linked").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        store.path("20260724-010000-linked")


def test_task_store_creates_complete_shape(tmp_path: Path) -> None:
    task_id = "20260724-010000-example"
    task = TaskStore(tmp_path).create(task_id, "Test goal")
    assert task.state is TaskState.SCOPED
    for name in ("request.md", "notes.md", "result.md", "task.json", "events.jsonl", "artifacts", "evidence", "delegations"):
        assert (tmp_path / "tasks" / task_id / name).exists()
    assert (tmp_path / "tasks" / task_id).stat().st_mode & 0o077 == 0


def test_lifecycle_requires_verification() -> None:
    assert transition(TaskState.NEW, TaskState.SCOPED) is TaskState.SCOPED
    with pytest.raises(ValueError):
        transition(TaskState.NEW, TaskState.COMPLETED)
    with pytest.raises(ValueError):
        transition(TaskState.VERIFYING, TaskState.COMPLETED)
    assert transition(TaskState.VERIFYING, TaskState.COMPLETED, verified=True) is TaskState.COMPLETED


def test_consequential_action_policy() -> None:
    assert not requires_authorization(ActionClass.OBSERVE)
    assert requires_authorization(ActionClass.COMMIT_EXTERNAL)
    assert retry_policy(ActionClass.COMMIT_EXTERNAL, "ambiguous") == "block"


def test_routing_is_deterministic_and_disclosure_gated() -> None:
    local = assess(RoutingInput(deterministic=True, dependent_steps=2, provider_available=True))
    assert local.decision == "local"
    suggested = assess(RoutingInput(
        architecture=True, dependent_steps=12, safety_review=True,
        relevant_files=8, provider_available=True, disclosure_authorized=False,
    ))
    assert suggested.decision == "suggest"
    assert "disclosure not authorized" in suggested.blocked_reasons
    delegated = assess(RoutingInput(
        architecture=True, dependent_steps=12, safety_review=True,
        relevant_files=8, provider_available=True, disclosure_authorized=True,
    ))
    assert delegated.decision == "delegate"


def test_scanner_finds_secrets_binary_and_task_material(tmp_path: Path) -> None:
    source = tmp_path / "safe.txt"
    source.write_text("api_key = 'abcdefghijklmnopqrstuvwxyz123456'", encoding="utf-8")
    assert any(item.kind == "api_token" for item in scan_file(tmp_path, source))
    binary = tmp_path / "image.bin"
    binary.write_bytes(b"a\0b")
    assert any(item.kind == "binary_denied" for item in scan_file(tmp_path, binary))
    task_file = tmp_path / "tasks" / "one" / "notes.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("notes", encoding="utf-8")
    assert any(item.kind == "task_material" for item in scan_file(tmp_path, task_file))


def test_task_store_rejects_tampered_identity_and_stale_transition(tmp_path: Path) -> None:
    task_id = "20260724-010000-example"
    store = TaskStore(tmp_path)
    store.create(task_id, "Goal")
    bound = store.bind(task_id)
    bound.transition(TaskState.SCOPED, TaskState.READY)
    with pytest.raises(ValueError, match="stale"):
        bound.transition(TaskState.SCOPED, TaskState.EXECUTING)
    metadata = tmp_path / "tasks" / task_id / "task.json"
    data = __import__("json").loads(metadata.read_text())
    data["task_id"] = "20260724-010001-other"
    metadata.write_text(__import__("json").dumps(data))
    with pytest.raises(ValueError, match="identity"):
        bound.load()


def test_delegation_is_bound_to_provider_context_and_request() -> None:
    request = DelegationRequest("task", "req", "chatgpt-web", "abc", "review")
    decision = DisclosureDecision("d", "task", "chatgpt-web", "abc", ("src",), "approved")
    validate_disclosure(decision, request)
    response = DelegationResponse("task", "req", "chatgpt-web", "abc", "review", {"verdict": "ready"})
    validate_response(request, response)
    with pytest.raises(ValueError):
        validate_response(request, DelegationResponse("task", "other", "chatgpt-web", "abc", "review", {}))


class FakeAdapter:
    adapter_id = "fake"

    def capabilities(self): return None
    def claim(self, task_id): return ("tab-1",)
    def observe(self, task_id, target):
        return BrowserObservation(task_id, None, target, {}, "pre", "tab-1")
    def act(self, action):
        return BrowserObservation(action.task_id, action.action_id, action.target, {"sent": "yes"}, "post", "tab-1")
    def capture(self, task_id, action_id):
        return BrowserObservation(task_id, action_id, "", {}, "capture", "tab-1")


def test_orchestrator_blocks_then_consumes_exact_grant(tmp_path: Path) -> None:
    task_id = "20260724-010000-example"
    store = TaskStore(tmp_path)
    store.create(task_id, "Goal")
    bound = store.bind(task_id)
    bound.transition(TaskState.SCOPED, TaskState.READY)
    action = BrowserAction("a", task_id, ActionClass.COMMIT_EXTERNAL, "https://example.test",
                           "send message", ({"type": "state_equals", "key": "sent", "value": "yes"},))
    orchestrator = Orchestrator(store, task_id, FakeAdapter())
    with pytest.raises(PermissionError):
        orchestrator.execute(action)
    grant = AuthorizationGrant("g", task_id, ActionClass.COMMIT_EXTERNAL, action.target,
                               summary_sha256(action), "2999-01-01T00:00:00+00:00")
    result = orchestrator.execute(action, grant)
    assert result.outcome == "verified"
    assert result.consumed_grant and result.consumed_grant.uses == 1
    with pytest.raises(ValueError, match="uses"):
        orchestrator.execute(action, grant)


def test_content_hash_is_not_wildcard() -> None:
    action = BrowserAction("a", "task", ActionClass.COMMIT_EXTERNAL, "target", "send", (), "payload")
    grant = AuthorizationGrant("g", "task", ActionClass.COMMIT_EXTERNAL, "target",
                               summary_sha256(action), "2999-01-01T00:00:00+00:00")
    from browser_tasks.authorization import validate_grant
    with pytest.raises(ValueError, match="content"):
        validate_grant(grant, action)


def test_scanner_rejects_parent_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert any(f.kind == "unsupported_type" for f in scan_file(root, root / "link" / "secret.txt"))
