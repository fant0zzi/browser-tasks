from __future__ import annotations

from .models import RoutingDecision, RoutingInput


def assess(value: RoutingInput) -> RoutingDecision:
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
        (value.web_research, 2, "web research requested"),
        (value.current_information, 2, "current information required"),
        (value.cross_source_synthesis, 2, "cross-source synthesis required"),
        (value.regulatory, 2, "regulatory analysis required"),
        (value.unfamiliar_domain, 1, "unfamiliar domain"),
        (value.large_research_volume, 2, "large or exhaustive research corpus"),
        (value.deterministic and value.dependent_steps <= 5, -3, "small deterministic flow"),
        (value.local_test_decides, -2, "direct local test can decide"),
        (value.live_observation_primary, -2, "primarily live-state observation"),
        (value.sensitive_broad_context, -2, "broad sensitive disclosure"),
    ]
    for enabled, weight, reason in weights:
        if enabled:
            score += weight
            reasons.append(reason)

    simple_local = (
        value.deterministic
        and value.dependent_steps <= 3
        and (value.local_test_decides or value.live_observation_primary)
        and not any((
            value.user_forced,
            value.architecture,
            value.ambiguity,
            value.safety_review,
            value.web_research,
            value.current_information,
            value.cross_source_synthesis,
            value.regulatory,
            value.unfamiliar_domain,
            value.deep_research_requested,
        ))
    )
    should_delegate = value.user_forced or (
        value.maximal_delegation and not simple_local
    ) or score >= 4
    if not should_delegate:
        return RoutingDecision(
            "local", score, tuple(reasons), research_mode="none"
        )

    if value.user_forced:
        reasons.append("user explicitly requested web delegation")
    elif value.maximal_delegation:
        reasons.append("maximal delegation policy")

    deep_research = value.deep_research_requested or (
        value.web_research
        and value.large_research_volume
        and any((
            value.cross_source_synthesis,
            value.regulatory,
            value.ambiguity,
            value.unfamiliar_domain,
            value.dependent_steps > 8,
        ))
    )
    research_mode = "deep" if deep_research else "standard"
    blocked: list[str] = []
    if value.requested_provider != "chatgpt-web":
        blocked.append("only chatgpt-web delegation is allowed")
    if value.requested_transport != "surf-ui":
        blocked.append("only surf-ui transport is allowed")
    if not value.provider_available:
        blocked.append("chatgpt-web provider unavailable")
    if not value.transport_available:
        blocked.append("surf-ui transport unavailable")
    if not value.disclosure_authorized:
        blocked.append("disclosure not authorized")
    if deep_research and not value.deep_research_available:
        blocked.append("deep research required but unavailable")
    if blocked:
        return RoutingDecision(
            "blocked",
            score,
            tuple(reasons),
            tuple(blocked),
            provider="chatgpt-web",
            transport="surf-ui",
            reasoning_effort="best",
            research_mode=research_mode,
            fallback_policy="block",
        )
    return RoutingDecision(
        "delegate",
        score,
        tuple(reasons),
        provider="chatgpt-web",
        transport="surf-ui",
        reasoning_effort="best",
        research_mode=research_mode,
        fallback_policy="block",
    )
