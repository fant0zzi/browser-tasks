from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

from .models import AuthorizationGrant, BrowserAction


def summary_sha256(action: BrowserAction) -> str:
    return hashlib.sha256(action.summary.encode("utf-8")).hexdigest()


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


def consume(grant: AuthorizationGrant, action: BrowserAction, now: datetime | None = None) -> AuthorizationGrant:
    validate_grant(grant, action, now)
    return replace(grant, uses=grant.uses + 1)
