from __future__ import annotations

from .models import RoutingDecision, RoutingInput


def assess(value: RoutingInput) -> RoutingDecision:
    if value.user_forced:
        if not value.provider_available:
            return RoutingDecision("blocked", 0, blocked_reasons=("provider unavailable",))
        if not value.disclosure_authorized:
            return RoutingDecision("blocked", 0, blocked_reasons=("disclosure not authorized",))
        return RoutingDecision("delegate", 99, reasons=("user explicitly requested web delegation",))

    score = 0
    reasons: list[str] = []
    weights = [
        (value.architecture, 2, "architecture or multi-system design"),
        (value.dependent_steps > 8, 2, "more than eight dependent steps"),
        (value.ambiguity, 2, "substantial ambiguity or branching"),
        (value.safety_review, 2, "safety, security, or privacy review"),
        (value.relevant_files > 5, 1, "more than five relevant files"),
        (value.repeated_failures, 1, "repeated local failure"),
        (value.substantial_final_review, 1, "final review of a substantial change"),
        (value.deterministic and value.dependent_steps <= 5, -3, "small deterministic flow"),
        (value.local_test_decides, -2, "direct local test can decide"),
        (value.live_observation_primary, -2, "primarily live-state observation"),
        (value.sensitive_broad_context, -2, "broad sensitive disclosure"),
    ]
    for enabled, weight, reason in weights:
        if enabled:
            score += weight
            reasons.append(reason)
    if score >= 4:
        blocked = []
        if not value.provider_available:
            blocked.append("provider unavailable")
        if not value.disclosure_authorized:
            blocked.append("disclosure not authorized")
        if blocked:
            return RoutingDecision("suggest", score, tuple(reasons), tuple(blocked))
        return RoutingDecision("delegate", score, tuple(reasons))
    return RoutingDecision("local", score, tuple(reasons))
