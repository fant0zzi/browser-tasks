from __future__ import annotations

from pathlib import Path

import pytest

from browser_tasks.lifecycle import transition
from browser_tasks.models import ActionClass, RoutingInput, TaskState
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
