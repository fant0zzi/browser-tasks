from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .models import RoutingInput
from .policy import ensure_external_tool_allowed
from .routing import assess
from .task_store import TaskStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="browser-tasks")
    root.add_argument("--root", type=Path, default=Path.cwd())
    commands = root.add_subparsers(dest="command", required=True)
    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    init = task_commands.add_parser("init")
    init.add_argument("task_id")
    init.add_argument("--goal", required=True)
    init.add_argument(
        "--delegation-policy", choices=("maximal", "off"), default="maximal"
    )
    init.add_argument("--browser-adapter", choices=("surf",), default="surf")
    init.add_argument(
        "--reasoning-effort", choices=("best", "high", "max"), default="best"
    )
    init.add_argument(
        "--deep-research", choices=("auto", "standard", "deep"), default="auto"
    )
    status = task_commands.add_parser("status")
    status.add_argument("task_id")
    enforce = task_commands.add_parser("enforce-policy")
    enforce.add_argument("task_id")
    route = commands.add_parser("route")
    route.add_argument("--architecture", action="store_true")
    route.add_argument("--steps", type=int, default=0)
    route.add_argument("--files", type=int, default=0)
    route.add_argument("--safety-review", action="store_true")
    route.add_argument("--ambiguity", action="store_true")
    route.add_argument("--web-research", action="store_true")
    route.add_argument("--current-information", action="store_true")
    route.add_argument("--cross-source-synthesis", action="store_true")
    route.add_argument("--regulatory", action="store_true")
    route.add_argument("--unfamiliar-domain", action="store_true")
    route.add_argument("--large-research-volume", action="store_true")
    route.add_argument("--deep-research", action="store_true")
    route.add_argument("--deep-research-unavailable", action="store_true")
    route.add_argument("--transport-unavailable", action="store_true")
    route.add_argument("--user-forced", action="store_true")
    route.add_argument("--deterministic", action="store_true")
    route.add_argument("--local-test-decides", action="store_true")
    route.add_argument("--live-observation-primary", action="store_true")
    route.add_argument("--no-maximal-delegation", action="store_true")
    route.add_argument("--requested-provider", default="chatgpt-web")
    route.add_argument("--requested-transport", default="surf-ui")
    route.add_argument("--disclosure-authorized", action="store_true")
    route.add_argument("--provider-unavailable", action="store_true")
    guard = commands.add_parser("guard")
    guard.add_argument("task_id")
    guard.add_argument(
        "--capability",
        choices=("browser", "reasoning", "research"),
        required=True,
    )
    guard.add_argument("--tool", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "task":
        store = TaskStore(args.root)
        if args.task_command == "init":
            task = store.create(
                args.task_id,
                args.goal,
                delegation_policy=args.delegation_policy,
                allowed_browser_adapters=(args.browser_adapter,),
                reasoning_effort=args.reasoning_effort,
                deep_research_policy=args.deep_research,
            )
        elif args.task_command == "enforce-policy":
            task = store.bind(args.task_id).enforce_delegate_first_policy()
        else:
            task = store.load(args.task_id)
        print(json.dumps(task.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.command == "guard":
        task = TaskStore(args.root).load(args.task_id)
        try:
            ensure_external_tool_allowed(task, args.capability, args.tool)
        except (PermissionError, ValueError) as error:
            print(json.dumps({
                "allowed": False,
                "capability": args.capability,
                "reason": str(error),
                "task_id": args.task_id,
                "tool": args.tool,
            }, indent=2, sort_keys=True))
            return 2
        print(json.dumps({
            "allowed": True,
            "capability": args.capability,
            "tool": args.tool,
            "task_id": args.task_id,
        }, indent=2, sort_keys=True))
        return 0
    decision = assess(RoutingInput(
        architecture=args.architecture,
        dependent_steps=args.steps,
        relevant_files=args.files,
        safety_review=args.safety_review,
        ambiguity=args.ambiguity,
        web_research=args.web_research,
        current_information=args.current_information,
        cross_source_synthesis=args.cross_source_synthesis,
        regulatory=args.regulatory,
        unfamiliar_domain=args.unfamiliar_domain,
        large_research_volume=args.large_research_volume,
        deep_research_requested=args.deep_research,
        deep_research_available=not args.deep_research_unavailable,
        transport_available=not args.transport_unavailable,
        user_forced=args.user_forced,
        deterministic=args.deterministic,
        local_test_decides=args.local_test_decides,
        live_observation_primary=args.live_observation_primary,
        maximal_delegation=not args.no_maximal_delegation,
        requested_provider=args.requested_provider,
        requested_transport=args.requested_transport,
        disclosure_authorized=args.disclosure_authorized,
        provider_available=not args.provider_unavailable,
    ))
    print(json.dumps(asdict(decision), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
