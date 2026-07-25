from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from .models import AuthorizationGrant, BrowserAction


EVIDENCE_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def canonical_postconditions(action: BrowserAction) -> str:
    return json.dumps(
        [dict(sorted(item.items())) for item in action.postconditions],
        sort_keys=True,
        separators=(",", ":"),
    )


def summary_sha256(action: BrowserAction) -> str:
    """Bind the human summary and the verification contract to one digest.

    Postconditions are part of what an operator authorizes: a grant approved
    for a strictly verified action must not be spendable on the same summary
    with the checks removed.
    """

    material = "\n".join(
        (
            "summary",
            action.summary,
            "postconditions",
            canonical_postconditions(action),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_evidence_digest(evidence_sha256: str) -> str:
    cleaned = evidence_sha256.strip().lower()
    if not EVIDENCE_DIGEST.fullmatch(cleaned):
        raise ValueError(
            "evidence must be the sha256 digest of a captured artifact"
        )
    return cleaned


def validate_grant(grant: AuthorizationGrant, action: BrowserAction, now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    expiry = datetime.fromisoformat(grant.expires_at)
    if expiry.tzinfo is None:
        raise ValueError("grant expiry must be timezone-aware")
    checks = {
        "task": grant.task_id == action.task_id,
        "class": grant.action_class == action.action_class,
        "target": grant.target == action.target,
        "summary": grant.summary_sha256 == summary_sha256(action),
        "content": grant.content_sha256 == action.content_sha256,
        "expiry": current < expiry,
        "uses": grant.uses < grant.max_uses,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError("invalid authorization grant: " + ", ".join(failed))
